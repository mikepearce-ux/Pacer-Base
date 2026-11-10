import psycopg
from psycopg.conninfo import make_conninfo

from conftest import admin_headers, auth, make_token


def new_client(api, name="S&S", **kw):
    r = api.post("/api/clients", json={"name": name, **kw}, headers=admin_headers())
    assert r.status_code == 201, r.text
    return r.json()


CALL = {
    "kind": "call",
    "title": "Weekly call 4 Nov",
    "summary": "Agreed to test a free-shipping threshold message. CVR flat at 2.1%.",
    "url": "https://notes.granola.ai/d/meeting-1",
    "source": "granola",
    "external_id": "meeting-1",
    "occurred_at": "2026-11-04T10:00:00Z",
}


def test_health_needs_no_token(api):
    assert api.get("/health").json()["ok"] is True


def test_auth_required(api):
    assert api.get("/api/clients").status_code == 401
    assert api.get("/api/clients", headers=auth("pb_wrong")).status_code == 401
    assert api.post("/api/clients", json={"name": "x"}).status_code == 401


def test_create_client_makes_its_own_database(api, settings):
    c = new_client(api, aliases=["SNS"], domains=["sns.example"])
    assert c["slug"] == "s-and-s"
    assert c["db_name"] == f"{settings.client_db_prefix}s_and_s"
    with psycopg.connect(settings.database_url) as conn:
        assert conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (c["db_name"],)).fetchone()
    with psycopg.connect(make_conninfo(settings.database_url, dbname=c["db_name"])) as conn:
        meta = dict(conn.execute("SELECT key, value FROM client_meta").fetchall())
    assert meta == {"slug": "s-and-s", "name": "S&S"}
    assert api.post("/api/clients", json={"name": "S&S"}, headers=admin_headers()).status_code == 409


def test_only_admin_creates_clients_and_tokens(api):
    writer = make_token(api, "write")
    assert api.post("/api/clients", json={"name": "x"}, headers=auth(writer)).status_code == 403
    assert api.post("/api/tokens", json={"label": "x"}, headers=auth(writer)).status_code == 403


def test_entry_create_then_update_by_external_id(api):
    new_client(api)
    tok = make_token(api, "write", label="mike")
    r = api.post("/api/clients/s-and-s/entries", json=CALL, headers=auth(tok))
    assert r.status_code == 201
    first = r.json()["entry"]
    assert first["created_by"] == "mike" and first["source"] == "granola"

    # Same source + external_id: updated in place, other fields kept.
    r = api.post(
        "/api/clients/S%26S/entries",
        json={"source": "granola", "external_id": "meeting-1", "summary": "Updated summary", "status": "done"},
        headers=auth(tok),
    )
    assert r.status_code == 200 and r.json()["action"] == "updated"
    second = r.json()["entry"]
    assert second["id"] == first["id"]
    assert second["summary"] == "Updated summary" and second["title"] == CALL["title"]
    assert len(api.get("/api/clients/s-and-s/entries", headers=auth(tok)).json()) == 1


def test_same_url_and_kind_updates_instead_of_duplicating(api):
    new_client(api)
    tok = make_token(api)
    doc = {"kind": "audit", "title": "CRO audit", "summary": "v1", "url": "https://docs.google.com/d/audit"}
    a = api.post("/api/clients/s-and-s/entries", json=doc, headers=auth(tok)).json()
    b = api.post("/api/clients/s-and-s/entries", json={**doc, "summary": "v2"}, headers=auth(tok)).json()
    assert a["entry"]["id"] == b["entry"]["id"] and b["entry"]["summary"] == "v2"
    # Same link but a different kind is a different entry (e.g. the audit and the call about it).
    c = api.post("/api/clients/s-and-s/entries", json={**doc, "kind": "call"}, headers=auth(tok)).json()
    assert c["action"] == "created"


def test_patch_archive_and_filters(api):
    new_client(api)
    tok = make_token(api)
    e = api.post("/api/clients/s-and-s/entries", json=CALL, headers=auth(tok)).json()["entry"]
    t = api.post(
        "/api/clients/s-and-s/entries",
        json={"kind": "test", "title": "PDP-04 sticky ATC", "summary": "Live on mobile PDP.", "status": "live", "tags": ["mobile"]},
        headers=auth(tok),
    ).json()["entry"]

    r = api.patch(f"/api/clients/s-and-s/entries/{t['id']}", json={"status": "finished"}, headers=auth(tok))
    assert r.json()["status"] == "finished" and r.json()["summary"] == "Live on mobile PDP."

    assert [x["id"] for x in api.get("/api/clients/s-and-s/entries?kind=test", headers=auth(tok)).json()] == [t["id"]]
    assert [x["id"] for x in api.get("/api/clients/s-and-s/entries?tag=mobile", headers=auth(tok)).json()] == [t["id"]]
    assert [x["id"] for x in api.get("/api/clients/s-and-s/entries?status=FINISHED", headers=auth(tok)).json()] == [t["id"]]
    since = api.get("/api/clients/s-and-s/entries?since=2026-11-05T00:00:00Z", headers=auth(tok)).json()
    assert e["id"] not in [x["id"] for x in since]

    assert api.delete(f"/api/clients/s-and-s/entries/{e['id']}", headers=auth(tok)).status_code == 200
    ids = [x["id"] for x in api.get("/api/clients/s-and-s/entries", headers=auth(tok)).json()]
    assert e["id"] not in ids
    ids = [x["id"] for x in api.get("/api/clients/s-and-s/entries?include_archived=true", headers=auth(tok)).json()]
    assert e["id"] in ids
    assert api.get("/api/clients/s-and-s/entries/999", headers=auth(tok)).status_code == 404


def test_search(api):
    new_client(api)
    tok = make_token(api)
    api.post("/api/clients/s-and-s/entries", json=CALL, headers=auth(tok))
    api.post(
        "/api/clients/s-and-s/entries",
        json={"kind": "report", "title": "October report", "summary": "Sessions up 12%; mobile conversion still lags desktop."},
        headers=auth(tok),
    )
    hits = api.get("/api/clients/s-and-s/entries?q=shipping thresholds", headers=auth(tok)).json()
    assert [h["title"] for h in hits] == ["Weekly call 4 Nov"]  # stemming: thresholds -> threshold
    assert "**" in hits[0]["snippet"]
    hits = api.get("/api/clients/s-and-s/entries?q=conversion&kind=report", headers=auth(tok)).json()
    assert [h["title"] for h in hits] == ["October report"]
    # Substring fallback for things full-text search doesn't index, like part of a link.
    hits = api.get("/api/clients/s-and-s/entries?q=granola.ai/d/meeting", headers=auth(tok)).json()
    assert [h["title"] for h in hits] == ["Weekly call 4 Nov"]


def test_validation_errors(api):
    new_client(api)
    tok = make_token(api)
    bad = [
        {**CALL, "url": "javascript:alert(1)"},
        {**CALL, "kind": "meeting"},
        {"kind": "call", "title": "No content"},
        {**CALL, "occurred_at": "2026-11-04T10:00:00"},
    ]
    for body in bad:
        assert api.post("/api/clients/s-and-s/entries", json=body, headers=auth(tok)).status_code == 422, body


def test_profile_sections_and_history(api, settings):
    c = new_client(api)
    tok = make_token(api, label="mike")
    r = api.put("/api/clients/s-and-s/profile/goals", json={"content": "- CVR 2.1% -> 2.4%"}, headers=auth(tok))
    assert r.status_code == 200 and r.json()["updated_by"] == "mike"
    api.put("/api/clients/s-and-s/profile/goals", json={"content": "- CVR 2.1% -> 2.5%"}, headers=auth(tok))
    profile = api.get("/api/clients/s-and-s/profile", headers=auth(tok)).json()
    assert profile["goals"]["content"] == "- CVR 2.1% -> 2.5%" and profile["people"] is None
    with psycopg.connect(make_conninfo(settings.database_url, dbname=c["db_name"])) as conn:
        assert conn.execute("SELECT content FROM profile_history").fetchall() == [("- CVR 2.1% -> 2.4%",)]
    assert api.put("/api/clients/s-and-s/profile/nonsense", json={"content": "x"}, headers=auth(tok)).status_code == 422

    md = api.get("/api/clients/s-and-s/context", headers=auth(tok)).json()["markdown"]
    assert "### Goals" in md and "2.5%" in md


def test_read_token_cannot_write(api):
    new_client(api)
    reader = make_token(api, "read")
    assert api.get("/api/clients/s-and-s/entries", headers=auth(reader)).status_code == 200
    assert api.post("/api/clients/s-and-s/entries", json=CALL, headers=auth(reader)).status_code == 403
    assert api.put("/api/clients/s-and-s/profile/goals", json={"content": "x"}, headers=auth(reader)).status_code == 403


def test_client_limited_token_cannot_see_other_clients(api):
    new_client(api, "S&S")
    new_client(api, "Other Brand")
    admin_writer = make_token(api)
    api.post("/api/clients/other-brand/entries", json=CALL, headers=auth(admin_writer))

    limited = make_token(api, "write", clients=["s-and-s"])
    slugs = [c["slug"] for c in api.get("/api/clients", headers=auth(limited)).json()]
    assert slugs == ["s-and-s"]
    # A client the token can't use looks exactly like one that doesn't exist.
    hidden = api.get("/api/clients/other-brand/entries", headers=auth(limited))
    missing = api.get("/api/clients/no-such-brand/entries", headers=auth(limited))
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json()["detail"].replace("other-brand", "X") == missing.json()["detail"].replace("no-such-brand", "X")
    assert "Other Brand" not in hidden.json()["detail"]
    assert api.post("/api/clients/other-brand/entries", json=CALL, headers=auth(limited)).status_code == 404
    assert api.post("/api/tokens", json={"label": "x", "clients": ["nope"]}, headers=admin_headers()).status_code == 422


def test_clients_are_isolated(api, settings):
    a = new_client(api, "S&S")
    b = new_client(api, "Other Brand")
    tok = make_token(api)
    api.post("/api/clients/s-and-s/entries", json=CALL, headers=auth(tok))
    assert api.get("/api/clients/other-brand/entries", headers=auth(tok)).json() == []
    assert api.get("/api/clients/other-brand/entries?q=shipping", headers=auth(tok)).json() == []
    # And physically: the other client's database has no rows at all.
    with psycopg.connect(make_conninfo(settings.database_url, dbname=b["db_name"])) as conn:
        assert conn.execute("SELECT count(*) FROM entries").fetchone() == (0,)
    with psycopg.connect(make_conninfo(settings.database_url, dbname=a["db_name"])) as conn:
        assert conn.execute("SELECT count(*) FROM entries").fetchone() == (1,)


def test_paused_client_is_read_only_and_archived_is_hidden(api):
    new_client(api)
    tok = make_token(api)
    r = api.patch("/api/clients/s-and-s", json={"status": "paused"}, headers=admin_headers())
    assert r.status_code == 200 and r.json()["status"] == "paused"
    assert api.get("/api/clients/s-and-s/entries", headers=auth(tok)).status_code == 200
    assert api.post("/api/clients/s-and-s/entries", json=CALL, headers=auth(tok)).status_code == 409
    api.patch("/api/clients/s-and-s", json={"status": "archived"}, headers=admin_headers())
    assert api.get("/api/clients", headers=auth(tok)).json() == []
    assert api.get("/api/clients/s-and-s/entries", headers=auth(tok)).status_code == 404
    assert api.patch("/api/clients/s-and-s", json={"aliases": "not a list"}, headers=admin_headers()).status_code == 422
    # An admin can still find an archived client to restore it.
    assert api.patch("/api/clients/s-and-s", json={"colour": "red"}, headers=admin_headers()).status_code == 422
    assert api.patch("/api/clients/s-and-s", json={"status": "active"}, headers=admin_headers()).status_code == 200
    assert api.post("/api/clients/s-and-s/entries", json=CALL, headers=auth(tok)).status_code == 201


def test_revoked_token_stops_working(api):
    tok = make_token(api, "read")
    tokens = api.get("/api/tokens", headers=admin_headers()).json()
    assert all("token_hash" not in t for t in tokens)
    tid = tokens[-1]["id"]
    assert api.get("/api/clients", headers=auth(tok)).status_code == 200
    assert api.delete(f"/api/tokens/{tid}", headers=admin_headers()).status_code == 200
    assert api.get("/api/clients", headers=auth(tok)).status_code == 401


def test_tokens_stored_hashed(api, settings):
    tok = make_token(api)
    with psycopg.connect(settings.database_url) as conn:
        stored = [r[0] for r in conn.execute("SELECT token_hash FROM api_tokens").fetchall()]
    assert tok not in stored and len(stored[0]) == 64


def test_restart_keeps_data_and_reapplies_nothing(settings):
    from fastapi.testclient import TestClient

    from pacer_base.app import create_app

    with TestClient(create_app(settings)) as api:
        new_client(api)
        tok = make_token(api)
        api.post("/api/clients/s-and-s/entries", json=CALL, headers=auth(tok))
    with TestClient(create_app(settings)) as api:
        assert len(api.get("/api/clients/s-and-s/entries", headers=auth(tok)).json()) == 1
