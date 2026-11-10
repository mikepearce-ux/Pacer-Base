"""The Pacer Base connector: MCP tools over Streamable HTTP."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field, ValidationError

from . import render, store
from .auth import PRINCIPAL_KEY, Principal
from .db import Database
from .models import KINDS, PROFILE_SECTIONS, EntryIn, Kind, ProfileSection

INSTRUCTIONS = f"""\
Pacer Base is Pacer's shared memory for each client: a profile (who's who, goals,
current tests, numbers we know, known issues, promises, key links) plus a timeline
of entries, each a short summary with a link to the real document.

Read before you work on a client: call get_client_context first, then
search_client_context or get_entry for anything specific. Treat what you find as
the team's notes, not ground truth: say where a figure came from and how old it is.

Write when something is created or decided: after you produce a deliverable
(results slide, audit, proposal, report, call notes), or when a test changes
state, call log_entry with the link and a 2-5 sentence summary that someone
could act on without opening the document. Re-logging the same url (or the same
source + external_id) updates the entry instead of duplicating it. Keep the
profile current with update_profile_section; it replaces the whole section, so
send the full new text.

Entry kinds: {render.kinds_help()}.
Each client is separate: never copy one client's information into another's.
"""

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
ARCHIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)

ClientRef = Annotated[
    str, Field(description="Client id (e.g. 's-and-s'), name or alias. list_clients shows them.")
]


def _principal(ctx: Context) -> Principal:
    request = getattr(ctx.request_context, "request", None)
    principal = getattr(request, "scope", {}).get(PRINCIPAL_KEY) if request is not None else None
    if not isinstance(principal, Principal):
        raise ToolError("Not authenticated")
    return principal


def _parse_when(value: str | None, field: str) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        when = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ToolError(f"{field} must be a date like 2026-11-04 or 2026-11-04T10:00:00Z") from exc
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def build_mcp(db: Database) -> MCPServer:
    mcp = MCPServer(
        name="pacer-base",
        title="Pacer Base",
        description="Per-client context for Pacer: profiles, plus summaries of and links to calls, tests, reports and docs.",
        instructions=INSTRUCTIONS,
        version="0.1.0",
    )

    async def client_for(ctx: Context, ref: str, needed: str = "read") -> tuple[Principal, dict]:
        who = _principal(ctx)
        if not who.has(needed):
            raise ToolError(f"This token is {who.scope}-only; {needed} access is needed for that.")
        try:
            client = await store.resolve_client(db, ref, who.allowed)
        except store.NotFound as exc:
            raise ToolError(str(exc)) from exc
        if client["status"] == "paused" and needed != "read":
            raise ToolError(f"{client['name']} is paused; it can be read but not changed.")
        return who, client

    @mcp.tool(annotations=READ, structured_output=False)
    async def list_clients(ctx: Context) -> str:
        """List the clients in Pacer Base that you can use, with how much is logged for each."""
        who = _principal(ctx)
        clients = await store.list_clients(db, who.allowed)
        stats = {}
        for c in clients:
            async with db.client(c["db_name"]) as conn:
                stats[c["slug"]] = await store.stats(conn)
        return render.client_list(clients, stats)

    @mcp.tool(annotations=READ, structured_output=False)
    async def get_client_context(
        client: ClientRef,
        ctx: Context,
        recent: Annotated[int, Field(ge=0, le=50, description="How many recent entries to include")] = 12,
    ) -> str:
        """Everything to know before working on a client: their profile and recent activity
        (calls, tests, reports, docs), each with a link. Call this first."""
        _, c = await client_for(ctx, client)
        async with db.client(c["db_name"]) as conn:
            profile = await store.get_profile(conn)
            entries = await store.list_entries(conn, limit=recent) if recent else []
            stats = await store.stats(conn)
        return render.client_context(c, profile, entries, stats)

    @mcp.tool(annotations=READ, structured_output=False)
    async def search_client_context(
        client: ClientRef,
        query: Annotated[str, Field(description="Words to find, e.g. 'free shipping threshold' or a test id")],
        ctx: Context,
        kinds: Annotated[list[Kind] | None, Field(description="Only these kinds of entry")] = None,
        since: Annotated[str | None, Field(description="Only entries on or after this date (YYYY-MM-DD)")] = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
    ) -> str:
        """Search one client's entries (titles, summaries, full text, tags). Best matches first."""
        _, c = await client_for(ctx, client)
        try:
            async with db.client(c["db_name"]) as conn:
                rows = await store.search_entries(
                    conn, query, kinds=list(kinds or []), since=_parse_when(since, "since"), limit=limit
                )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        if not rows:
            return f"Nothing in {c['name']} matches {query!r}."
        lines = [f"{len(rows)} result(s) for {query!r} in {c['name']}:"]
        lines += [render.entry_line(r, snippet=True) for r in rows]
        return "\n".join(lines)

    @mcp.tool(annotations=READ, structured_output=False)
    async def list_entries(
        client: ClientRef,
        ctx: Context,
        kinds: Annotated[list[Kind] | None, Field(description="Only these kinds of entry")] = None,
        since: Annotated[str | None, Field(description="On or after this date (YYYY-MM-DD)")] = None,
        until: Annotated[str | None, Field(description="On or before this date (YYYY-MM-DD)")] = None,
        tag: str | None = None,
        status: Annotated[str | None, Field(description="e.g. live, finished, draft")] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> str:
        """A client's timeline, newest first, filtered by kind, date, tag or status
        (e.g. every live test, or all calls since September)."""
        _, c = await client_for(ctx, client)
        try:
            async with db.client(c["db_name"]) as conn:
                rows = await store.list_entries(
                    conn,
                    kinds=list(kinds or []),
                    since=_parse_when(since, "since"),
                    until=_parse_when(until, "until"),
                    tag=tag,
                    status=status,
                    limit=limit,
                    offset=offset,
                )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        if not rows:
            return f"No entries in {c['name']} match those filters."
        more = f"\n_Showing {offset + 1}-{offset + len(rows)}; pass offset={offset + len(rows)} for more._" if len(rows) == limit else ""
        return "\n".join(render.entry_line(r) for r in rows) + more

    @mcp.tool(annotations=READ, structured_output=False)
    async def get_entry(
        client: ClientRef,
        entry_id: Annotated[int, Field(description="The #id shown next to an entry")],
        ctx: Context,
    ) -> str:
        """The full text of one entry: summary, detail, link, who logged it and when."""
        _, c = await client_for(ctx, client)
        try:
            async with db.client(c["db_name"]) as conn:
                row = await store.get_entry(conn, entry_id)
        except store.NotFound as exc:
            raise ToolError(f"{exc} in {c['name']}") from exc
        return render.entry_full(c, row)

    @mcp.tool(annotations=WRITE, structured_output=False)
    async def log_entry(
        client: ClientRef,
        ctx: Context,
        kind: Annotated[Kind | None, Field(description="; ".join(f"{k}: {v}" for k, v in KINDS.items()))] = None,
        title: Annotated[str | None, Field(description="Short and specific, e.g. 'PDP-04 sticky ATC: results'")] = None,
        summary: Annotated[
            str | None,
            Field(description="2-5 sentences someone could act on without opening the link: what it is, key numbers, decisions, next steps"),
        ] = None,
        url: Annotated[str | None, Field(description="Link to the document, slide, test or recording")] = None,
        body: Annotated[str | None, Field(description="Optional longer detail: full notes, actions, figures")] = None,
        status: Annotated[str | None, Field(description="Optional, e.g. planned, live, finished, draft, sent")] = None,
        tags: list[str] | None = None,
        occurred_at: Annotated[str | None, Field(description="When it happened (YYYY-MM-DD or ISO time). Default: now")] = None,
        source: Annotated[str | None, Field(description="Where it came from, e.g. claude, granola, intelligems, drive")] = None,
        external_id: Annotated[
            str | None, Field(description="The item's id in its source system (e.g. Intelligems test id), so later updates land on the same entry")
        ] = None,
        metadata: Annotated[dict[str, Any] | None, Field(description="Optional extra facts as key/value pairs")] = None,
        entry_id: Annotated[int | None, Field(description="Set to update an existing entry; only the fields you pass change")] = None,
    ) -> str:
        """Record something that was created, happened or was decided for a client, with a
        link and a summary. Use it after making a deliverable, after a call, or when a test
        changes state. Updates instead of duplicating when the url or external_id is already logged."""
        who, c = await client_for(ctx, client, "write")
        try:
            data = EntryIn(
                kind=kind,
                title=title,
                summary=summary,
                body=body,
                url=url,
                status=status,
                source=source,
                external_id=external_id,
                tags=tags,
                metadata=metadata,
                occurred_at=_parse_when(occurred_at, "occurred_at"),
            )
            async with db.client(c["db_name"]) as conn:
                row, action = await store.save_entry(
                    conn, data, who.label, entry_id=entry_id, default_source="claude"
                )
        except ValidationError as exc:
            raise ToolError("; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())) from exc
        except (ValueError, store.NotFound, store.Conflict) as exc:
            raise ToolError(str(exc)) from exc
        return f"{action.capitalize()} entry #{row['id']} in {c['name']}:\n" + render.entry_line(row)

    @mcp.tool(annotations=WRITE, structured_output=False)
    async def update_profile_section(
        client: ClientRef,
        section: Annotated[ProfileSection, Field(description="; ".join(f"{k}: {v}" for k, v in PROFILE_SECTIONS.items()))],
        content: Annotated[str, Field(description="The complete new text of the section (markdown). Replaces what's there; the old text is kept in history.")],
        ctx: Context,
    ) -> str:
        """Replace one section of a client's profile (the living brief). Read the current
        text with get_client_context first and send the whole updated section."""
        who, c = await client_for(ctx, client, "write")
        try:
            async with db.client(c["db_name"]) as conn:
                row = await store.set_profile_section(conn, section, content, who.label)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return f"Updated '{PROFILE_SECTIONS[row['key']]}' for {c['name']} ({len(row['content'])} characters)."

    @mcp.tool(annotations=ARCHIVE, structured_output=False)
    async def archive_entry(
        client: ClientRef,
        entry_id: int,
        ctx: Context,
    ) -> str:
        """Hide an entry that's wrong or no longer useful (it's kept, not deleted, and can be
        restored by an admin)."""
        who, c = await client_for(ctx, client, "write")
        try:
            async with db.client(c["db_name"]) as conn:
                row = await store.archive_entry(conn, entry_id, who.label)
        except store.NotFound as exc:
            raise ToolError(f"{exc} in {c['name']}") from exc
        return f"Archived entry #{row['id']} ({row['title']}) in {c['name']}."

    return mcp
