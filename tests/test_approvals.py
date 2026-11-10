"""Proposals and approval, sources, settings and limits, through the REST API."""

import psycopg
from psycopg.conninfo import make_conninfo

from conftest import admin_headers, auth, make_token

APPROVERS = [
    {"name": "Robin Park", "slack_user_id": "U0AAAAAAAA1"},
    {"name": "Jo Bloggs", "slack_user_id": "U0AAAAAAAA2"},
]
BASE = "/api/clients/acme"


def setup_client(api):
    r = api.post("/api/clients", json={"name": "Acme", "aliases": ["AC"], "domains": ["acme.example"]}, headers=admin_headers())
    assert r.status_code == 201, r.text
    assert api.put("/api/settings/approvers", json={"value": APPROVERS}, headers=admin_headers()).status_code == 200
    return r.json()


def propose(api, tok, content="- Oct CVR: 2.3% (GA4)", summary="Oct CVR 2.1% -> 2.3%", keys=None, section="numbers"):
    return api.post(
        f"{BASE}/proposals",
        json={
            "section": section,
            "proposed_content": content,
            "change_summary": summary,
            "reason": "Dev in the internal channel, 10 Nov",
            "evidence_url": "https://acme.slack.com/archives/C0TEST/p1",
            "source_keys": keys or ["slack:C0TEST:1731249000.1"],
        },
        headers=auth(tok),
    )


def decide(api, tok, pid, decision="approve", by="Jo Bloggs", **kw):
    return api.post(f"{BASE}/proposals/{pid}/decision", json={"decision": decision, "decided_by": by, "via": "slack reaction", **kw}, headers=auth(tok))


def profile(api, tok, section="numbers"):
    row = api.get(f"{BASE}/profile", headers=auth(tok)).json()[section]
    return row["content"] if row else None


def test_proposal_waits_then_approval_applies_it(api):
    setup_client(api)
    writer = make_token(api, "write", label="daily-sync")
    approver = make_token(api, "approve", label="morning-check")

    r = propose(api, writer)
    assert r.status_code == 201
    p = r.json()["proposal"]
    assert p["status"] == "pending" and p["base_content"] == ""
    assert profile(api, writer) is None  # nothing applied yet

    ctx = api.get(f"{BASE}/context", headers=auth(writer)).json()
    assert "Waiting for approval" in ctx["markdown"] and "Oct CVR 2.1% -> 2.3%" in ctx["markdown"]

    # Writers can't approve; approvers can, but only as a named approver.
    assert decide(api, writer, p["id"]).status_code == 403
    r = decide(api, approver, p["id"], by="Sarah")
    assert r.status_code == 403 and "isn't an approver" in r.json()["detail"]

    r = decide(api, approver, p["id"], by="U0AAAAAAAA2")  # Slack id works too
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "approved" and done["decided_by"] == "Jo Bloggs"
    assert "slack reaction" in done["decided_via"] and "morning-check" in done["decided_via"]
    assert profile(api, writer) == "- Oct CVR: 2.3% (GA4)"
    prof = api.get(f"{BASE}/profile", headers=auth(writer)).json()["numbers"]
    assert prof["updated_by"] == f"Jo Bloggs (approved P{p['id']})"

    assert decide(api, approver, p["id"]).status_code == 409  # already decided
    assert "Waiting for approval" not in api.get(f"{BASE}/context", headers=auth(writer)).json()["markdown"]


def test_correction_is_applied_instead(api):
    setup_client(api)
    writer, approver = make_token(api, "write"), make_token(api, "approve")
    p = propose(api, writer).json()["proposal"]
    r = decide(api, approver, p["id"], corrected_content="- Oct CVR: 2.25% (GA4)", note="Jo: it's 2.25")
    assert r.json()["applied_content"] == "- Oct CVR: 2.25% (GA4)"
    assert profile(api, writer) == "- Oct CVR: 2.25% (GA4)"


def test_rejected_source_is_not_proposed_again(api):
    setup_client(api)
    writer, approver = make_token(api, "write"), make_token(api, "approve")
    p = propose(api, writer).json()["proposal"]
    assert decide(api, approver, p["id"], "reject").json()["status"] == "rejected"
    assert profile(api, writer) is None
    r = propose(api, writer, content="- Oct CVR: 2.4%", keys=["slack:C0TEST:1731249000.1", "granola:m-2"])
    assert r.status_code == 409 and "rejected before" in r.json()["detail"]
    assert propose(api, writer, content="- Oct CVR: 2.4%", keys=["granola:m-3"]).status_code == 201


def test_new_proposal_for_a_section_replaces_the_pending_one(api):
    setup_client(api)
    writer, approver = make_token(api, "write"), make_token(api, "approve")
    first = propose(api, writer).json()["proposal"]
    r = propose(api, writer, content="- Oct CVR: 2.3%\n- Oct AOV: £52", summary="CVR and AOV", keys=["slack:C0TEST:2"])
    assert r.json()["replaced"] == [first["id"]]
    statuses = {p["id"]: p["status"] for p in api.get(f"{BASE}/proposals?status=all", headers=auth(writer)).json()}
    assert statuses[first["id"]] == "superseded"
    assert decide(api, approver, first["id"]).status_code == 409
    # A different section is independent.
    assert propose(api, writer, section="promises", content="- Q1 roadmap by 20 Nov", summary="promise").json()["replaced"] == []


def test_approval_refused_if_section_changed_meanwhile(api):
    setup_client(api)
    writer, approver = make_token(api, "write"), make_token(api, "approve")
    p = propose(api, writer).json()["proposal"]
    api.put(f"{BASE}/profile/numbers", json={"content": "- edited by hand"}, headers=auth(approver))
    r = decide(api, approver, p["id"])
    assert r.status_code == 409 and "changed after" in r.json()["detail"]
    assert profile(api, writer) == "- edited by hand"


def test_no_change_and_bad_section_rejected(api):
    setup_client(api)
    writer, approver = make_token(api, "write"), make_token(api, "approve")
    api.put(f"{BASE}/profile/numbers", json={"content": "- Oct CVR: 2.3% (GA4)"}, headers=auth(approver))
    assert propose(api, writer).status_code == 422
    assert propose(api, writer, section="nonsense").status_code == 422


def test_proposals_expire(api, settings):
    c = setup_client(api)
    writer, approver = make_token(api, "write"), make_token(api, "approve")
    p = propose(api, writer).json()["proposal"]
    with psycopg.connect(make_conninfo(settings.database_url, dbname=c["db_name"]), autocommit=True) as conn:
        conn.execute("UPDATE proposals SET proposed_at = now() - interval '8 days'")
    rows = api.get(f"{BASE}/proposals?status=all", headers=auth(writer)).json()
    assert rows[0]["status"] == "expired"
    assert decide(api, approver, p["id"]).status_code == 409


def test_sources_admin_only_and_validated(api):
    setup_client(api)
    writer = make_token(api, "write")
    sources = {
        "sources": [
            {"system": "slack", "ref": "C0EXT00001", "name": "#pacer-acme", "role": "shared with client"},
            {"system": "slack", "ref": "C0INT00001", "name": "#internal-acme", "role": "internal"},
            {"system": "drive", "ref": "1abcDEF", "name": "Clients/Acme", "url": "https://drive.google.com/drive/folders/1abcDEF", "role": "approved documents"},
            {"system": "monday", "ref": "5100000000", "name": "Acme board"},
            {"system": "granola", "ref": "acme.example", "role": "attendee email domain"},
        ]
    }
    assert api.put(f"{BASE}/sources", json=sources, headers=auth(writer)).status_code == 403
    r = api.put(f"{BASE}/sources", json=sources, headers=admin_headers())
    assert r.status_code == 200 and len(r.json()) == 5
    assert [x["ref"] for x in api.get(f"{BASE}/sources", headers=auth(writer)).json()][0] == "1abcDEF"
    dup = {"sources": [sources["sources"][0], sources["sources"][0]]}
    assert api.put(f"{BASE}/sources", json=dup, headers=admin_headers()).status_code == 422
    bad = {"sources": [{"system": "myspace", "ref": "x"}]}
    assert api.put(f"{BASE}/sources", json=bad, headers=admin_headers()).status_code == 422
    # Replacing the list drops what's not in it.
    r = api.put(f"{BASE}/sources", json={"sources": sources["sources"][:1]}, headers=admin_headers())
    assert len(r.json()) == 1
    md = api.get(f"{BASE}/context", headers=auth(writer)).json()["markdown"]
    assert "## Where to look" in md and "#pacer-acme" in md and "C0EXT00001" in md


def test_settings_validated_and_admin_only(api):
    writer = make_token(api, "write")
    assert api.get("/api/settings", headers=auth(writer)).json()["daily_entry_cap"] == 50
    assert api.put("/api/settings/daily_entry_cap", json={"value": 10}, headers=auth(writer)).status_code == 403
    assert api.put("/api/settings/daily_entry_cap", json={"value": 10}, headers=admin_headers()).json() == {"daily_entry_cap": 10}
    for key, value in [
        ("approvers", [{"name": "x", "slack_user_id": "not-an-id"}]),
        ("review_channel", {"id": "nope"}),
        ("daily_entry_cap", 0),
        ("made_up", 1),
    ]:
        assert api.put(f"/api/settings/{key}", json={"value": value}, headers=admin_headers()).status_code == 422, key
    ok = {"id": "C0REVIEW01", "name": "#pacer-base-review"}
    assert api.put("/api/settings/review_channel", json={"value": ok}, headers=admin_headers()).status_code == 200
    assert api.get("/api/settings", headers=auth(writer)).json()["review_channel"]["name"] == "#pacer-base-review"


def test_daily_cap_on_new_entries(api):
    setup_client(api)
    api.put("/api/settings/daily_entry_cap", json={"value": 2}, headers=admin_headers())
    writer = make_token(api, "write", label="daily-sync")
    other = make_token(api, "write", label="someone-else")

    def entry(i):
        return {"kind": "note", "title": f"n{i}", "summary": "s", "source": "slack", "external_id": f"C1:{i}"}

    for i in range(2):
        assert api.post(f"{BASE}/entries", json=entry(i), headers=auth(writer)).status_code == 201
    r = api.post(f"{BASE}/entries", json=entry(3), headers=auth(writer))
    assert r.status_code == 422 and "Daily limit" in r.json()["detail"]
    # Updates don't count, other writers have their own allowance, admins aren't capped.
    assert api.post(f"{BASE}/entries", json={**entry(0), "summary": "updated"}, headers=auth(writer)).status_code == 200
    assert api.post(f"{BASE}/entries", json=entry(4), headers=auth(other)).status_code == 201
    assert api.post(f"{BASE}/entries", json=entry(5), headers=admin_headers()).status_code == 201


def test_related_links_round_trip(api):
    setup_client(api)
    writer = make_token(api)
    body = {
        "kind": "test",
        "title": "Free-shipping message",
        "summary": "Build done; launching Thursday.",
        "related": [
            {"label": "Monday item", "url": "https://pacer.monday.com/boards/1/pulses/2"},
            {"label": "Slack thread", "url": "https://acme.slack.com/archives/C1/p2"},
        ],
    }
    e = api.post(f"{BASE}/entries", json=body, headers=auth(writer)).json()["entry"]
    assert [r["label"] for r in e["related"]] == ["Monday item", "Slack thread"]
    md = api.get(f"{BASE}/context", headers=auth(writer)).json()["markdown"]
    assert "Related: Monday item https://pacer.monday.com/boards/1/pulses/2" in md
    bad = {**body, "related": [{"label": "x", "url": "javascript:1"}]}
    assert api.post(f"{BASE}/entries", json=bad, headers=auth(writer)).status_code == 422
