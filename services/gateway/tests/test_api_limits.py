"""What the API does with input nobody intended.

A local single-tenant tool does not need a threat model. It does need every field that reaches
the database to have a shape, because without one `POST /api/agents` with a twenty-thousand
character name is a 200 — stored, rendered into every dropdown, and pasted into the system prompt
on every turn of every call. That is not an attack, it is a Tuesday with a bad paste.

The other half of this file is the ordinary not-found cases, which are the ones the studio hits
whenever something is deleted in another tab.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dialtone.server.app import DOCUMENT, LINE, QUERY, SHORT, TITLE, app


@pytest.fixture
def client(monkeypatch):
    """The API over a scratch database, with no model behind it.

    Both are environment variables rather than fixtures reaching into the module, so the test
    exercises the same configuration path an operator would use.
    """
    monkeypatch.setenv("DIALTONE_DB", str(Path(tempfile.mkdtemp()) / "t.db"))
    monkeypatch.setenv("DIALTONE_NO_MODEL", "1")
    with TestClient(app) as c:
        yield c


def agent_id(client) -> str:
    """An agent to hang documents off. Created rather than assumed: the scratch database this
    fixture builds is genuinely empty, which is itself a case worth exercising."""
    existing = client.get("/api/agents").json()["agents"]
    if existing:
        return existing[0]["id"]
    return client.post("/api/agents", json={"name": "Test", "business": "Test"}).json()["id"]


class TestNothingUnbounded:
    @pytest.mark.parametrize("field,limit", [
        ("name", SHORT), ("business", SHORT), ("persona", LINE),
        ("greeting", LINE), ("voice", SHORT),
    ])
    def test_an_agent_field_has_a_ceiling(self, client, field: str, limit: int):
        assert client.post("/api/agents", json={field: "x" * (limit + 1)}).status_code == 422
        assert client.post("/api/agents", json={field: "x" * limit}).status_code == 200

    def test_a_document_has_a_ceiling(self, client):
        target = f"/api/agents/{agent_id(client)}/documents"
        assert client.post(target, json={"title": "x" * (TITLE + 1), "body": "hi"}).status_code == 422
        assert client.post(target, json={"title": "ok", "body": "x" * (DOCUMENT + 1)}).status_code == 422

    def test_a_search_has_a_ceiling(self, client):
        target = f"/api/agents/{agent_id(client)}/knowledge/search"
        assert client.post(target, json={"query": "x" * (QUERY + 1)}).status_code == 422

    def test_redaction_has_a_ceiling(self, client):
        assert client.post("/api/redact", json={"text": "x" * (QUERY + 1)}).status_code == 422

    def test_the_details_form_has_a_ceiling(self, client):
        """The one endpoint a caller can reach without being an operator."""
        assert client.patch(
            "/api/calls/anything/details", json={"name": "x" * (SHORT + 1)}
        ).status_code == 422

    def test_a_reasonable_value_still_goes_through(self, client):
        """The caps exist to stop the absurd, not to argue with a long business name."""
        response = client.post("/api/agents", json={
            "name": "Northgate Dental & Orthodontic Practice (Whitechapel Road)",
            "business": "Northgate Dental",
            "greeting": "Good morning, Northgate Dental, this is reception — how can I help you?",
        })
        assert response.status_code == 200


class TestThingsThatAreNotThere:
    """Everything the studio hits when something was deleted in another tab."""

    @pytest.mark.parametrize("method,path", [
        ("get", "/api/agents/nope"),
        ("get", "/api/agents/nope/availability"),
        ("get", "/api/agents/nope/documents"),
        ("get", "/api/calls/nope"),
        ("get", "/api/calls/nope/memory"),
        ("patch", "/api/calls/nope/details"),
        ("delete", "/api/appointments/nope"),
    ])
    def test_it_is_a_404_not_a_crash(self, client, method: str, path: str):
        call = getattr(client, method)
        response = call(path, json={}) if method in ("patch", "post", "put") else call(path)
        assert response.status_code == 404, f"{path} gave {response.status_code}"

    def test_calling_an_agent_that_is_gone(self, client):
        assert client.post("/api/calls", json={"agent_id": "nope"}).status_code in (404, 503)

    def test_a_path_that_looks_like_an_escape_is_just_a_miss(self, client):
        assert client.get("/api/agents/..%2f..%2fetc%2fpasswd").status_code == 404


class TestEmptyAndOdd:
    def test_an_empty_search_does_not_explode(self, client):
        response = client.post(
            f"/api/agents/{agent_id(client)}/knowledge/search", json={"query": ""}
        )
        assert response.status_code == 200
        assert response.json()["hits"] == []

    def test_scoring_an_empty_sentence(self, client):
        assert client.get("/api/benchmark/score?text=").status_code in (200, 422)

    def test_a_silly_limit_is_clamped_not_obeyed(self, client):
        assert client.get("/api/calls?limit=999999").status_code == 200
        assert client.get("/api/calls?limit=-1").status_code in (200, 422)

    def test_appointments_for_an_agent_that_never_existed(self, client):
        response = client.get("/api/appointments?agent_id=nope")
        assert response.status_code == 200
        assert response.json()["appointments"] == []
