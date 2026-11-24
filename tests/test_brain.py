"""The brain: connecting entries, flags, the picture, and the spend cap.

Claude is replaced by a fake client that returns canned answers and usage, so
these tests cost nothing and check what Pacer Base does with the answers.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from conftest import admin_headers, auth, make_token


class FakeMessages:
    def __init__(self):
        self.calls: list[dict] = []
        self.connect_answer: dict = {"links": [], "tags": [], "entities": {"people": [], "tests": [], "products": [], "competitors": []}, "flags": []}
        self.picture_answer = "## Where things stand\nAll good [#1]."

    async def create(self, **kw):
        self.calls.append(kw)
        is_connect = "format" in kw.get("output_config", {})
        text = json.dumps(self.connect_answer) if is_connect else self.picture_answer
        usage = SimpleNamespace(input_tokens=10_000, output_tokens=2_000, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], usage=usage,
                               stop_reason="end_turn", model=kw["model"])


class FakeClaude:
    def __init__(self):
        self.messages = FakeMessages()
        self.beta = SimpleNamespace(messages=self.messages)


@pytest.fixture
def brain_api(settings):
    from fastapi.testclient import TestClient

    from pacer_base.app import create_app

    fake = FakeClaude()
    with TestClient(create_app(settings, brain_client=fake)) as client:
        client.fake = fake
        yield client


def _setup(api):
    api.post("/api/clients", json={"name": "Acme"}, headers=admin_headers())
    tok = make_token(api, "write", label="Pacer team")
    post = lambda body: api.post("/api/clients/acme/entries", json=body, headers=auth(tok)).json()["entry"]
    a = post({"kind": "test", "title": "PDP-04 sticky add to basket: live", "summary": "Sticky ATC on mobile PDP, live from 1 Nov."})
    b = post({"kind": "call", "title": "Weekly call 10 Nov", "summary": "Client asked about the sticky add to basket test."})
    return tok, a, b


def test_connects_entries_records_spend_and_shows_it(brain_api):
    tok, a, b = _setup(brain_api)
    fake = brain_api.fake.messages
    fake.connect_answer = {
        "links": [{"id": a["id"], "relation": "same_test", "reason": "Asks about PDP-04.", "confidence": 0.9},
                  {"id": 999, "relation": "same_topic", "reason": "not a candidate", "confidence": 0.95},
                  {"id": a["id"], "relation": "same_topic", "reason": "weak", "confidence": 0.3}],
        "tags": ["PDP-04", "Sticky ATC!"],
        "entities": {"people": ["Sam"], "tests": ["PDP-04"], "products": [], "competitors": []},
        "flags": [{"kind": "loose_end", "text": "Client's question wasn't answered.", "entry_ids": [b["id"]]}],
    }
    r = brain_api.post("/api/brain/run", headers=admin_headers())
    assert r.status_code == 200, r.text
    assert r.json()["connected"] == 2
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == "default"
    assert call["output_config"]["effort"] == "low" and call["output_config"]["format"]["type"] == "json_schema"
    # Only real candidates above the bar are linked; tags are cleaned and merged.
    entry = brain_api.get(f"/api/clients/acme/entries/{b['id']}", headers=auth(tok)).json()
    assert "pdp-04" in entry["tags"] and "sticky-atc" in entry["tags"]
    assert entry["metadata"]["brain_entities"]["people"] == ["Sam"]

    status = brain_api.get("/api/brain/status", headers=admin_headers()).json()
    assert status["clients"]["acme"]["waiting_to_connect"] == 0 and status["clients"]["acme"]["open_flags"] >= 1
    # 2 connects + the first picture: each 10k in / 2k out on Opus 5.5 = $0.08
    assert status["month_spend_usd"] == pytest.approx(0.24, abs=0.01)


def test_connector_shows_picture_flags_and_connections(brain_api):
    tok, a, b = _setup(brain_api)
    fake = brain_api.fake.messages
    fake.connect_answer = {
        "links": [{"id": a["id"], "relation": "same_test", "reason": "Asks about PDP-04.", "confidence": 0.9}],
        "tags": [], "entities": {"people": [], "tests": [], "products": [], "competitors": []},
        "flags": [{"kind": "risk", "text": "Test results due before Black Friday.", "entry_ids": []}],
    }
    fake.picture_answer = "## Where things stand\nPDP-04 is live [#1]; the client is keen [#2]."
    assert brain_api.post("/api/brain/run?picture=true", headers=admin_headers()).json()["pictures"] == 1

    import asyncio

    # Read through the REST context helpers the connector uses.
    from pacer_base import brain as brain_mod, render, store

    async def render_ctx():
        db = brain_api.app.state.db
        c = await store.resolve_client(db, "acme", None)
        async with db.client(c["db_name"]) as conn:
            picture = await brain_mod.get_picture(conn)
            flags = await brain_mod.open_flags(conn)
            links = await brain_mod.connections(conn, a["id"])
            profile = await store.get_profile(conn)
            stats = await store.stats(conn)
            row = await store.get_entry(conn, a["id"])
        return render.client_context(c, profile, [], stats, [], [], picture, flags), render.entry_full(c, row, links)

    ctx, entry = brain_api.portal.call(render_ctx) if hasattr(brain_api, "portal") else asyncio.run(render_ctx())
    assert "What Pacer Base has pieced together" in ctx and "PDP-04 is live [#1]" in ctx
    assert "Test results due before Black Friday." in ctx
    # A link made from #2 to #1 shows on #1 too.
    assert f"[#{b['id']}] Weekly call 10 Nov" in entry and "same test" in entry


def test_reconnects_only_when_content_changes(brain_api):
    tok, a, b = _setup(brain_api)
    brain_api.post("/api/brain/run", headers=admin_headers())
    n = len(brain_api.fake.messages.calls)
    # Re-logging identical content (like the portal re-syncing) costs nothing.
    brain_api.patch(f"/api/clients/acme/entries/{a['id']}", json={"status": "live"}, headers=auth(tok))
    brain_api.post("/api/brain/run", headers=admin_headers())
    assert len(brain_api.fake.messages.calls) == n
    brain_api.patch(f"/api/clients/acme/entries/{a['id']}", json={"summary": "Ended: +4% conversion."}, headers=auth(tok))
    brain_api.post("/api/brain/run", headers=admin_headers())
    assert len(brain_api.fake.messages.calls) == n + 1


def test_stops_at_the_budget_and_when_switched_off(brain_api):
    _setup(brain_api)
    brain_api.put("/api/settings/brain_monthly_budget_usd", json={"value": 0.05}, headers=admin_headers())
    r = brain_api.post("/api/brain/run", headers=admin_headers()).json()
    assert r["connected"] == 1  # the first call takes spend to $0.08, over the $0.05 cap
    assert len(brain_api.fake.messages.calls) == 1
    brain_api.put("/api/settings/brain_monthly_budget_usd", json={"value": 100}, headers=admin_headers())
    brain_api.put("/api/settings/brain_enabled", json={"value": False}, headers=admin_headers())
    assert brain_api.post("/api/brain/run", headers=admin_headers()).json() == {"connected": 0, "pictures": 0}
    assert len(brain_api.fake.messages.calls) == 1


def test_brain_off_without_a_key(api):
    r = api.post("/api/brain/run", headers=admin_headers())
    assert r.status_code == 409
    assert api.get("/api/brain/status", headers=admin_headers()).json()["configured"] is False
