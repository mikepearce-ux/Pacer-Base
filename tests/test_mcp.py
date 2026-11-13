"""The connector end to end: a real uvicorn server and the real MCP client."""

import asyncio

import httpx
import httpx2
from fastapi.testclient import TestClient  # noqa: F401  (keeps httpx test deps explicit)
from mcp import Client
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from conftest import ADMIN

TOOLS = {
    "list_clients",
    "get_client_context",
    "search_client_context",
    "list_entries",
    "get_entry",
    "log_entry",
    "archive_entry",
    "propose_profile_change",
    "list_proposals",
    "attach_review_message",
    "decide_proposal",
    "update_profile_section",
    "get_sync_plan",
    "mark_source_checked",
}


def admin(base, method, path, json=None):
    r = httpx.request(method, base + path, json=json, headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code < 300, r.text
    return r.json()


def token(base, scope="write", clients=None, label="claude-connector"):
    return admin(base, "POST", "/api/tokens", {"label": label, "scope": scope, "clients": clients})["token"]


def text(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


async def call(url, name, args=None):
    async with Client(url) as c:
        return await c.call_tool(name, args or {})


def test_full_flow_with_token_in_url(live_server):
    admin(live_server, "POST", "/api/clients", {"name": "S&S", "aliases": ["SNS"], "domains": ["sns.example"]})
    url = f"{live_server}/mcp/{token(live_server, 'approve')}"

    async def flow():
        async with Client(url) as c:
            tools = await c.list_tools()
            assert {t.name for t in tools.tools} == TOOLS
            by_name = {t.name: t for t in tools.tools}
            assert by_name["get_client_context"].annotations.read_only_hint is True
            assert by_name["log_entry"].annotations.read_only_hint is False

            listing = text(await c.call_tool("list_clients", {}))
            assert "`s-and-s`" in listing and "SNS" in listing

            r = await c.call_tool(
                "log_entry",
                {
                    "client": "S&S",
                    "kind": "call",
                    "title": "Weekly call 4 Nov",
                    "summary": "Agreed to test a free-shipping threshold message.",
                    "url": "https://notes.granola.ai/d/m-1",
                    "source": "granola",
                    "external_id": "m-1",
                    "occurred_at": "2026-11-04",
                },
            )
            assert not r.is_error and "Created entry #1" in text(r)
            # Syncing the same Granola meeting again updates it.
            r = await c.call_tool(
                "log_entry",
                {"client": "sns", "source": "granola", "external_id": "m-1", "summary": "Agreed to test it from 18 Nov."},
            )
            assert "Updated entry #1" in text(r)
            # Logging the meeting's link from a chat joins Granola's entry and says so.
            r = await c.call_tool(
                "log_entry",
                {"client": "sns", "kind": "call", "title": "The call", "summary": "Mine.",
                 "url": "https://notes.granola.ai/d/m-1", "related": [{"label": "Thread", "url": "https://x.example/t"}]},
            )
            assert "already entry #1" in text(r) and "made by granola" in text(r)

            r = await c.call_tool(
                "update_profile_section",
                {"client": "s-and-s", "section": "numbers", "content": "- CVR Oct 2026: 2.1% (GA4)"},
            )
            assert not r.is_error

            ctx = text(await c.call_tool("get_client_context", {"client": "s-and-s"}))
            assert "### Numbers we know" in ctx and "2.1%" in ctx
            assert "[#1] **Weekly call 4 Nov**" in ctx and "18 Nov" in ctx
            assert "domains: sns.example" in ctx

            found = text(await c.call_tool("search_client_context", {"client": "s-and-s", "query": "agreed to test"}))
            assert "[#1]" in found
            none = text(await c.call_tool("search_client_context", {"client": "s-and-s", "query": "podcast"}))
            assert "Nothing" in none

            full = text(await c.call_tool("get_entry", {"client": "s-and-s", "entry_id": 1}))
            assert "Source: granola · external id: m-1" in full and "by claude-connector" in full

            listed = text(await c.call_tool("list_entries", {"client": "s-and-s", "kinds": ["call"], "since": "2026-11-01"}))
            assert "[#1]" in listed

            r = await c.call_tool("archive_entry", {"client": "s-and-s", "entry_id": 1})
            assert "Archived entry #1" in text(r)
            assert "Nothing logged yet" in text(await c.call_tool("get_client_context", {"client": "s-and-s"}))

    asyncio.run(flow())


def test_tool_errors_are_readable(live_server):
    admin(live_server, "POST", "/api/clients", {"name": "S&S"})
    url = f"{live_server}/mcp/{token(live_server)}"

    async def flow():
        async with Client(url) as c:
            r = await c.call_tool("get_client_context", {"client": "Nobody"})
            assert r.is_error and "No client matches 'Nobody'" in text(r) and "s-and-s" in text(r)
            r = await c.call_tool("get_entry", {"client": "s-and-s", "entry_id": 42})
            assert r.is_error and "No entry 42" in text(r)
            r = await c.call_tool("log_entry", {"client": "s-and-s", "kind": "call", "title": "x", "url": "not-a-url"})
            assert r.is_error and "url" in text(r)
            r = await c.call_tool("log_entry", {"client": "s-and-s", "kind": "call", "title": "no content"})
            assert r.is_error and "needs at least" in text(r)
            r = await c.call_tool("list_entries", {"client": "s-and-s", "since": "last tuesday"})
            assert r.is_error and "since must be a date" in text(r)

    asyncio.run(flow())


def test_bearer_header_works(live_server):
    admin(live_server, "POST", "/api/clients", {"name": "S&S"})
    tok = token(live_server, "read")

    async def flow():
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {tok}"}) as http:
            async with streamable_http_client(f"{live_server}/mcp", http_client=http) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    r = await session.call_tool("list_clients", {})
                    assert "`s-and-s`" in text(r)

    asyncio.run(flow())


def test_bad_tokens_rejected(live_server):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"Accept": "application/json, text/event-stream"}
    assert httpx.post(f"{live_server}/mcp", json=body, headers=headers).status_code == 401
    assert httpx.post(f"{live_server}/mcp/pb_nope", json=body, headers=headers).status_code == 401
    bad = {**headers, "Authorization": "Bearer pb_nope"}
    r = httpx.post(f"{live_server}/mcp", json=body, headers=bad)
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
    assert httpx.get(f"{live_server}/somewhere-else").status_code == 404


def test_read_only_and_client_limited_tokens(live_server):
    admin(live_server, "POST", "/api/clients", {"name": "S&S"})
    admin(live_server, "POST", "/api/clients", {"name": "Other Brand"})

    async def flow():
        ro = f"{live_server}/mcp/{token(live_server, 'read')}"
        r = await call(ro, "log_entry", {"client": "s-and-s", "kind": "note", "title": "x", "summary": "y"})
        assert r.is_error and "read access; write access is needed" in text(r)
        rw = f"{live_server}/mcp/{token(live_server, 'write')}"
        r = await call(rw, "update_profile_section", {"client": "s-and-s", "section": "goals", "content": "x"})
        assert r.is_error and "approve access is needed" in text(r) and "propose_profile_change" in text(r)

        limited = f"{live_server}/mcp/{token(live_server, 'write', clients=['s-and-s'])}"
        listing = text(await call(limited, "list_clients"))
        assert "s-and-s" in listing and "other-brand" not in listing
        r = await call(limited, "get_client_context", {"client": "other-brand"})
        assert r.is_error and text(r).endswith("Clients you can use: s-and-s.")
        assert "Other Brand" not in text(r)

    asyncio.run(flow())


def test_works_behind_railway_with_older_protocol_clients(live_server):
    """Railway forwards its own domain as Host; the SDK's localhost-only
    DNS-rebinding guard must not be on. Plain JSON-RPC at an older protocol
    version (what some clients still send) must work too."""
    admin(live_server, "POST", "/api/clients", {"name": "S&S"})
    tok = token(live_server, "read")
    headers = {
        "Accept": "application/json, text/event-stream",
        "Host": "pacer-base-production.up.railway.app",
        "Origin": "https://claude.ai",
    }
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    }
    r = httpx.post(f"{live_server}/mcp/{tok}", json=init, headers=headers)
    assert r.status_code == 200 and "Pacer Base" in r.json()["result"]["instructions"]
    call_ = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_clients", "arguments": {}}}
    r = httpx.post(f"{live_server}/mcp/{tok}", json=call_, headers={**headers, "mcp-protocol-version": "2025-06-18"})
    assert r.status_code == 200 and "s-and-s" in r.json()["result"]["content"][0]["text"]


def test_roundup_and_morning_check_flow(live_server):
    """What the scheduled runs do: read the plan, log a card with links, mark the
    source read, propose a profile change, record its review message, then an
    approve-level run applies it for a named approver."""
    import json

    admin(live_server, "POST", "/api/clients", {"name": "Acme", "aliases": ["AC"], "domains": ["acme.example"]})
    admin(live_server, "PUT", "/api/settings/approvers", {"value": [{"name": "Jo Bloggs", "slack_user_id": "U0AAAAAAAA2"}]})
    admin(live_server, "PUT", "/api/settings/review_channel", {"value": {"id": "C0REVIEW01", "name": "#pacer-base-review"}})
    admin(live_server, "PUT", "/api/settings/agency_channels", {"value": [{"id": "C0ALL00001", "name": "#pacer-all"}]})
    admin(live_server, "PUT", "/api/clients/acme/sources", {"sources": [
        {"system": "slack", "ref": "C0INT00001", "name": "#internal-acme", "role": "internal"},
        {"system": "granola", "ref": "acme.example", "role": "attendee email domain"},
    ]})
    sync = f"{live_server}/mcp/{token(live_server, 'write', label='daily-sync')}"
    check = f"{live_server}/mcp/{token(live_server, 'approve', label='morning-check')}"

    async def flow():
        async with Client(sync) as c:
            plan = json.loads(text(await c.call_tool("get_sync_plan", {"client": "AC"})))
            assert [s["key"] for s in plan["sources"]] == ["granola:acme.example", "slack:C0INT00001"]
            assert plan["agency_channels"][0]["key"] == "slack:C0ALL00001"
            assert "only save messages about Acme" in plan["agency_channels"][0]["rule"]
            assert plan["review_channel"]["id"] == "C0REVIEW01" and plan["approvers"][0]["name"] == "Jo Bloggs"
            assert plan["sources"][0]["read_from"] < plan["now"]  # first run looks back a week

            r = await c.call_tool("log_entry", {
                "client": "acme", "kind": "note", "title": "#internal-acme, 10 Nov",
                "summary": "Dev says the free-shipping build is done; launch Thursday.",
                "source": "slack", "external_id": "C0INT00001:2026-11-10",
                "url": "https://acme.slack.com/archives/C0INT00001/p1",
                "related": [{"label": "Monday item", "url": "https://pacer.monday.com/boards/1/pulses/2"}],
            })
            assert not r.is_error, text(r)

            r = await c.call_tool("mark_source_checked", {"client": "acme", "system": "slack", "ref": "C0INT00001", "checked_until": plan["now"]})
            assert not r.is_error
            r = await c.call_tool("mark_source_checked", {"client": "acme", "system": "slack", "ref": "C0ALL00001", "checked_until": plan["now"]})
            assert not r.is_error  # agency channels can be marked too
            r = await c.call_tool("mark_source_checked", {"client": "acme", "system": "slack", "ref": "C0SOMEWHERE", "checked_until": plan["now"]})
            assert r.is_error and "isn't one of" in text(r)
            again = json.loads(text(await c.call_tool("get_sync_plan", {"client": "acme"})))
            by_key = {s["key"]: s["read_from"] for s in again["sources"]}
            assert by_key["slack:C0INT00001"][:19] == plan["now"][:19]

            r = await c.call_tool("propose_profile_change", {
                "client": "acme", "section": "current_tests",
                "proposed_content": "- Free-shipping message: build done, launching Thursday",
                "change_summary": "Free-shipping test: ready to launch",
                "reason": "Dev in #internal-acme, 10 Nov",
                "evidence_url": "https://acme.slack.com/archives/C0INT00001/p1",
                "source_keys": ["slack:C0INT00001:1731249000.1"],
            })
            assert "Proposed P1" in text(r)
            r = await c.call_tool("decide_proposal", {"client": "acme", "proposal_id": 1, "decision": "approve", "decided_by": "Jo Bloggs"})
            assert r.is_error and "approve access is needed" in text(r)
            r = await c.call_tool("attach_review_message", {"client": "acme", "proposal_id": 1, "channel_id": "C0REVIEW01", "message_ts": "1731300000.0001"})
            assert not r.is_error
            ctx = text(await c.call_tool("get_client_context", {"client": "acme"}))
            assert "Waiting for approval" in ctx and "posted for review" in ctx
            assert "Related: Monday item" in ctx and "## Where to look" in ctx

        async with Client(check) as c:
            pending = text(await c.call_tool("list_proposals", {"client": "acme"}))
            assert "[P1]" in pending and "ts 1731300000.0001" in pending and "Proposed text:" in pending
            r = await c.call_tool("decide_proposal", {"client": "acme", "proposal_id": 1, "decision": "approve", "decided_by": "Sarah"})
            assert r.is_error and "isn't an approver" in text(r)
            r = await c.call_tool("decide_proposal", {"client": "acme", "proposal_id": 1, "decision": "approve", "decided_by": "U0AAAAAAAA2", "via": "slack reaction"})
            assert "approved by Jo Bloggs" in text(r), text(r)
            ctx = text(await c.call_tool("get_client_context", {"client": "acme"}))
            assert "### Current tests" in ctx and "launching Thursday" in ctx and "Waiting for approval" not in ctx

    asyncio.run(flow())
