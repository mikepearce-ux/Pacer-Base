"""Reads and writes. Registry functions take the Database; content functions take
one client's connection, so they can only ever touch that client's data."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable

import psycopg
from psycopg.types.json import Jsonb

from pydantic import TypeAdapter

from .db import Database, client_db_name
from .models import (
    KINDS,
    MAX_RELATED,
    PROFILE_SECTIONS,
    SETTING_DEFAULTS,
    SETTING_TYPES,
    ClientIn,
    DecisionIn,
    EntryIn,
    ProposalIn,
    RelatedLink,
    SourceIn,
    slugify,
)


class NotFound(LookupError):
    pass


class Forbidden(PermissionError):
    pass


class Conflict(ValueError):
    pass


# --------------------------------------------------------------------------
# Registry (control database)
# --------------------------------------------------------------------------

CLIENT_COLUMNS = "slug, name, aliases, domains, db_name, status, created_at, updated_at"


async def list_clients(
    db: Database, allowed: Iterable[str] | None = None, include_archived: bool = False
) -> list[dict]:
    query = f"SELECT {CLIENT_COLUMNS} FROM clients"
    if not include_archived:
        query += " WHERE status <> 'archived'"
    query += " ORDER BY name"
    async with db.control.connection() as conn:
        rows = await (await conn.execute(query)).fetchall()
    if allowed is not None:
        allowed = set(allowed)
        rows = [r for r in rows if r["slug"] in allowed]
    return rows


async def get_client(db: Database, slug: str) -> dict:
    async with db.control.connection() as conn:
        cur = await conn.execute(f"SELECT {CLIENT_COLUMNS} FROM clients WHERE slug = %s", (slug,))
        row = await cur.fetchone()
    if row is None:
        raise NotFound(f"No client {slug!r}")
    return row


async def resolve_client(
    db: Database, ref: str, allowed: Iterable[str] | None = None, include_archived: bool = False
) -> dict:
    """Find a client by slug, name or alias ('S&S', 's-and-s', 'Sand and Sky'...).

    Clients outside ``allowed`` are reported as not found, so a restricted token
    can't discover which other clients exist.
    """
    ref = (ref or "").strip()
    if not ref:
        raise NotFound("No client given. Call list_clients to see the clients you can use.")
    clients = await list_clients(db, allowed, include_archived=include_archived)
    lowered = ref.lower()
    as_slug = slugify(ref)
    for match in (
        lambda c: c["slug"] == lowered,
        lambda c: c["name"].lower() == lowered or lowered in (a.lower() for a in c["aliases"]),
        lambda c: c["slug"] == as_slug or slugify(c["name"]) == as_slug,
    ):
        found = [c for c in clients if match(c)]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            names = ", ".join(f"{c['name']} ({c['slug']})" for c in found)
            raise NotFound(f"{ref!r} matches more than one client: {names}. Use the slug.")
    names = ", ".join(c["slug"] for c in clients) or "none yet"
    raise NotFound(f"No client matches {ref!r}. Clients you can use: {names}.")


async def create_client(db: Database, data: ClientIn) -> dict:
    slug = data.resolved_slug()
    db_name = client_db_name(db.settings, slug)
    async with db.control.connection() as conn:
        try:
            cur = await conn.execute(
                f"INSERT INTO clients (slug, name, aliases, domains, db_name)"
                f" VALUES (%s, %s, %s, %s, %s) RETURNING {CLIENT_COLUMNS}",
                (slug, data.name, data.aliases, data.domains, db_name),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise Conflict(f"A client with slug {slug!r} already exists") from exc
        row = await cur.fetchone()
    await db.ensure_client_database(db_name)
    async with db.client(db_name) as conn:
        await conn.execute(
            "INSERT INTO client_meta (key, value) VALUES ('slug', %s), ('name', %s)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (slug, data.name),
        )
    return row


async def update_client(
    db: Database,
    slug: str,
    *,
    name: str | None = None,
    aliases: list[str] | None = None,
    domains: list[str] | None = None,
    status: str | None = None,
) -> dict:
    if status is not None and status not in ("active", "paused", "archived"):
        raise ValueError("status must be active, paused or archived")
    async with db.control.connection() as conn:
        cur = await conn.execute(
            f"UPDATE clients SET name = coalesce(%s, name), aliases = coalesce(%s, aliases),"
            f" domains = coalesce(%s, domains), status = coalesce(%s, status), updated_at = now()"
            f" WHERE slug = %s RETURNING {CLIENT_COLUMNS}",
            (name, aliases, domains, status, slug),
        )
        row = await cur.fetchone()
    if row is None:
        raise NotFound(f"No client {slug!r}")
    return row


# --------------------------------------------------------------------------
# Agency-wide settings (control database)
# --------------------------------------------------------------------------


async def get_settings(db: Database) -> dict[str, Any]:
    async with db.control.connection() as conn:
        rows = await (await conn.execute("SELECT key, value FROM settings")).fetchall()
    out = dict(SETTING_DEFAULTS)
    out.update({r["key"]: r["value"] for r in rows if r["key"] in SETTING_TYPES})
    return out


async def set_setting(db: Database, key: str, value: Any, actor: str) -> Any:
    if key not in SETTING_TYPES:
        raise ValueError(f"Unknown setting {key!r}. Settings: {', '.join(SETTING_TYPES)}")
    adapter = TypeAdapter(SETTING_TYPES[key])
    clean = adapter.dump_python(adapter.validate_python(value), mode="json")
    if key in ("daily_entry_cap", "proposal_expiry_days") and not 1 <= clean <= 1000:
        raise ValueError(f"{key} must be between 1 and 1000")
    async with db.control.connection() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value, updated_by) VALUES (%s, %s, %s)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now(),"
            " updated_by = excluded.updated_by",
            (key, Jsonb(clean), actor),
        )
    return clean


def match_approver(approvers: list[dict], who: str) -> str | None:
    """The approver's name if ``who`` is one of them (by name or Slack id)."""
    who = (who or "").strip().lower()
    for a in approvers:
        if who in (a["name"].lower(), a["slack_user_id"].lower()):
            return a["name"]
    return None


# --------------------------------------------------------------------------
# Client content (one client's database)
# --------------------------------------------------------------------------

ENTRY_COLUMNS = (
    "id, kind, title, summary, body, url, status, source, external_id, tags, metadata, related,"
    " occurred_at, created_at, updated_at, created_by, updated_by, archived_at"
)
LIST_COLUMNS = (
    "id, kind, title, summary, url, status, source, external_id, tags, related, occurred_at,"
    " updated_at, created_by, archived_at"
)


async def get_profile(conn: psycopg.AsyncConnection) -> dict[str, dict]:
    rows = await (await conn.execute("SELECT key, content, updated_at, updated_by FROM profile_sections")).fetchall()
    return {r["key"]: r for r in rows}


async def set_profile_section(
    conn: psycopg.AsyncConnection, key: str, content: str, actor: str
) -> dict:
    if key not in PROFILE_SECTIONS:
        raise ValueError(f"Unknown section {key!r}. Sections: {', '.join(PROFILE_SECTIONS)}")
    content = content.strip()
    async with conn.transaction():
        cur = await conn.execute(
            "SELECT content, updated_at, updated_by FROM profile_sections WHERE key = %s FOR UPDATE", (key,)
        )
        old = await cur.fetchone()
        if old and old["content"] and old["content"] != content:
            await conn.execute(
                "INSERT INTO profile_history (key, content, updated_at, updated_by) VALUES (%s, %s, %s, %s)",
                (key, old["content"], old["updated_at"], old["updated_by"]),
            )
        cur = await conn.execute(
            "INSERT INTO profile_sections (key, content, updated_at, updated_by) VALUES (%s, %s, now(), %s)"
            " ON CONFLICT (key) DO UPDATE SET content = excluded.content, updated_at = now(),"
            " updated_by = excluded.updated_by"
            " RETURNING key, content, updated_at, updated_by",
            (key, content, actor),
        )
        return await cur.fetchone()


async def _find_existing(conn: psycopg.AsyncConnection, e: EntryIn, source: str) -> dict | None:
    """The entry this write should update rather than duplicate, if there is one.

    First the same source + external_id; failing that, the same url and kind.
    The link match skips entries this same source made under a different
    external_id: one source can log several things under one link (Monday
    notes for different days all link the board).
    """
    if e.external_id:
        cur = await conn.execute(
            "SELECT id, source, external_id, related, tags FROM entries"
            " WHERE source = %s AND external_id = %s FOR UPDATE",
            (source, e.external_id),
        )
        row = await cur.fetchone()
        if row:
            return row
    if not (e.url and e.kind):
        return None
    sql = (
        "SELECT id, source, external_id, related, tags FROM entries"
        " WHERE url = %s AND kind = %s AND archived_at IS NULL"
    )
    params: list[Any] = [e.url, e.kind]
    if e.external_id:
        sql += " AND NOT (source = %s AND external_id IS NOT NULL AND external_id <> %s)"
        params += [source, e.external_id]
    cur = await conn.execute(sql + " ORDER BY id LIMIT 1 FOR UPDATE", params)
    return await cur.fetchone()


def _join(e: EntryIn, existing: dict, source: str) -> tuple[EntryIn, str]:
    """Fold a write into an entry that another source logged first under the same link.

    Something is often logged twice from different places: a portal page or heat
    map shared in Slack, then logged by the system that made it. There is one
    entry for it either way:

    - Same source, or neither side has an external_id: an ordinary update.
    - The write has an external_id and the entry has none (it was logged from a
      shared link): the entry takes the write's content, source and
      external_id, so that source finds it directly next time.
    - The entry has an external_id from another source: it belongs to that
      system, which rewrites it on its own schedule, so it keeps its content,
      source and external_id.

    In the last two cases the related links and tags of both are kept.
    Returns the write to apply and the action ("updated" or "linked").
    """
    if existing["source"] == source or not (e.external_id or existing["external_id"]):
        return e, "updated"
    related, seen = [], set()
    for link in [*(e.related or []), *(RelatedLink(**r) for r in existing["related"] or [])]:
        if link.url not in seen:
            seen.add(link.url)
            related.append(link)
    tags = list(dict.fromkeys([*(existing["tags"] or []), *(e.tags or [])]))
    if existing["external_id"]:
        return EntryIn(related=related[:MAX_RELATED], tags=tags), "linked"
    return e.model_copy(update={"source": source, "related": related[:MAX_RELATED], "tags": tags}), "updated"


async def save_entry(
    conn: psycopg.AsyncConnection,
    e: EntryIn,
    actor: str,
    entry_id: int | None = None,
    default_source: str = "manual",
    daily_cap: int | None = None,
) -> tuple[dict, str]:
    """Create an entry, or update one. Returns (entry, "created" | "updated" | "linked").

    Without ``entry_id``, an existing entry is updated instead of duplicated when
    it has the same source + external_id, or the same url and kind (see
    ``_find_existing`` and ``_join``). That makes re-running an import or a skill
    safe, and keeps one entry for a thing logged from two places. The action is
    "linked" when the entry belongs to another system and only gained related
    links and tags. ``default_source`` is only used for a new entry (and to
    match external_id); an update never changes the source unless one is
    passed. ``daily_cap`` limits how many new entries one writer can add to this
    client in 24 hours (updates don't count).
    """
    source = e.source or default_source
    async with conn.transaction():
        if entry_id is not None:
            return await _update_entry(conn, entry_id, e, actor), "updated"
        existing = await _find_existing(conn, e, source)
        if existing is not None:
            e, action = _join(e, existing, source)
            return await _update_entry(conn, existing["id"], e, actor), action
        e.require_new()
        if daily_cap is not None:
            cur = await conn.execute(
                "SELECT count(*) AS n FROM entries WHERE created_by = %s"
                " AND created_at > now() - interval '24 hours'",
                (actor,),
            )
            if (await cur.fetchone())["n"] >= daily_cap:
                raise Conflict(
                    f"Daily limit reached: {daily_cap} new entries for this client from '{actor}' in 24 hours."
                    " Updates still work; ask an admin if the limit needs raising."
                )
        cur = await conn.execute(
            f"INSERT INTO entries (kind, title, summary, body, url, status, source, external_id, tags,"
            f" metadata, related, occurred_at, created_by, updated_by)"
            f" VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, coalesce(%s, now()), %s, %s)"
            f" RETURNING {ENTRY_COLUMNS}",
            (
                e.kind,
                e.title,
                e.summary or "",
                e.body or "",
                e.url,
                e.status,
                source,
                e.external_id,
                e.tags or [],
                Jsonb(e.metadata or {}),
                Jsonb([r.model_dump() for r in e.related or []]),
                e.occurred_at,
                actor,
                actor,
            ),
        )
        return await cur.fetchone(), "created"


async def _update_entry(conn: psycopg.AsyncConnection, entry_id: int, e: EntryIn, actor: str) -> dict:
    fields: dict[str, Any] = e.model_dump(exclude_none=True)
    sets, params = [], []
    for name, value in fields.items():
        if name in ("metadata", "related"):
            value = Jsonb(value)
        sets.append(f"{name} = %s")
        params.append(value)
    sets += ["updated_at = now()", "updated_by = %s"]
    params += [actor, entry_id]
    try:
        cur = await conn.execute(
            f"UPDATE entries SET {', '.join(sets)} WHERE id = %s RETURNING {ENTRY_COLUMNS}", params
        )
    except psycopg.errors.UniqueViolation as exc:
        raise Conflict("Another entry already has that source and external_id") from exc
    row = await cur.fetchone()
    if row is None:
        raise NotFound(f"No entry {entry_id}")
    return row


async def get_entry(conn: psycopg.AsyncConnection, entry_id: int) -> dict:
    cur = await conn.execute(f"SELECT {ENTRY_COLUMNS} FROM entries WHERE id = %s", (entry_id,))
    row = await cur.fetchone()
    if row is None:
        raise NotFound(f"No entry {entry_id}")
    return row


async def archive_entry(conn: psycopg.AsyncConnection, entry_id: int, actor: str) -> dict:
    cur = await conn.execute(
        f"UPDATE entries SET archived_at = now(), updated_at = now(), updated_by = %s"
        f" WHERE id = %s RETURNING {ENTRY_COLUMNS}",
        (actor, entry_id),
    )
    row = await cur.fetchone()
    if row is None:
        raise NotFound(f"No entry {entry_id}")
    return row


def _filters(
    kinds: list[str] | None,
    since: datetime | None,
    until: datetime | None,
    tag: str | None,
    status: str | None,
    include_archived: bool,
) -> tuple[list[str], list[Any]]:
    where, params = [], []
    if not include_archived:
        where.append("archived_at IS NULL")
    if kinds:
        bad = [k for k in kinds if k not in KINDS]
        if bad:
            raise ValueError(f"Unknown kind(s) {bad}. Kinds: {', '.join(KINDS)}")
        where.append("kind = ANY(%s)")
        params.append(list(kinds))
    if since:
        where.append("occurred_at >= %s")
        params.append(since)
    if until:
        where.append("occurred_at <= %s")
        params.append(until)
    if tag:
        where.append("%s = ANY(tags)")
        params.append(tag.strip().lower())
    if status:
        where.append("lower(status) = lower(%s)")
        params.append(status)
    return where, params


async def list_entries(
    conn: psycopg.AsyncConnection,
    *,
    kinds: list[str] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    tag: str | None = None,
    status: str | None = None,
    include_archived: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    where, params = _filters(kinds, since, until, tag, status, include_archived)
    query = f"SELECT {LIST_COLUMNS} FROM entries"
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY occurred_at DESC, id DESC LIMIT %s OFFSET %s"
    params += [max(1, min(limit, 100)), max(0, offset)]
    return await (await conn.execute(query, params)).fetchall()


async def search_entries(
    conn: psycopg.AsyncConnection,
    query: str,
    *,
    kinds: list[str] | None = None,
    since: datetime | None = None,
    limit: int = 10,
) -> list[dict]:
    """Full-text search (English stemming, ranked); falls back to a plain
    substring match for things stemming doesn't catch, like test IDs."""
    query = (query or "").strip()
    if not query:
        raise ValueError("Search needs a query")
    limit = max(1, min(limit, 50))
    where, params = _filters(kinds, since, None, None, None, False)
    extra = (" AND " + " AND ".join(where)) if where else ""
    rows = await (
        await conn.execute(
            f"SELECT {LIST_COLUMNS},"
            f" ts_headline('english', summary || E'\\n' || left(body, 20000), q,"
            f"   'MaxWords=40, MinWords=15, MaxFragments=2, StartSel=**, StopSel=**') AS snippet,"
            f" ts_rank_cd(search, q) AS rank"
            f" FROM entries, websearch_to_tsquery('english', %s) AS q"
            f" WHERE search @@ q{extra}"
            f" ORDER BY rank DESC, occurred_at DESC LIMIT %s",
            [query, *params, limit],
        )
    ).fetchall()
    if rows:
        return rows
    like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return await (
        await conn.execute(
            f"SELECT {LIST_COLUMNS}, left(summary, 300) AS snippet, 0 AS rank FROM entries"
            f" WHERE (title ILIKE %s OR summary ILIKE %s OR body ILIKE %s OR url ILIKE %s"
            f"        OR external_id ILIKE %s){extra}"
            f" ORDER BY occurred_at DESC LIMIT %s",
            [like, like, like, like, like, *params, limit],
        )
    ).fetchall()


async def stats(conn: psycopg.AsyncConnection) -> dict:
    rows = await (
        await conn.execute(
            "SELECT kind, count(*) AS n, max(updated_at) AS last FROM entries"
            " WHERE archived_at IS NULL GROUP BY kind"
        )
    ).fetchall()
    profile_last = await (await conn.execute("SELECT max(updated_at) AS last FROM profile_sections")).fetchone()
    lasts = [r["last"] for r in rows if r["last"]] + ([profile_last["last"]] if profile_last["last"] else [])
    order = list(KINDS)
    rows.sort(key=lambda r: order.index(r["kind"]) if r["kind"] in order else len(order))
    return {
        "entries": sum(r["n"] for r in rows),
        "by_kind": {r["kind"]: r["n"] for r in rows},
        "last_updated": max(lasts) if lasts else None,
    }


# -- sources ----------------------------------------------------------------

SOURCE_COLUMNS = "id, system, ref, name, url, role, notes"


async def list_sources(conn: psycopg.AsyncConnection) -> list[dict]:
    return await (await conn.execute(f"SELECT {SOURCE_COLUMNS} FROM sources ORDER BY system, id")).fetchall()


async def replace_sources(conn: psycopg.AsyncConnection, sources: list[SourceIn]) -> list[dict]:
    """Set the full list (how far each has been read is kept separately)."""
    keys = [(x.system, x.ref) for x in sources]
    if len(set(keys)) != len(keys):
        raise ValueError("Each source (system + ref) can only be listed once")
    async with conn.transaction():
        existing = {(r["system"], r["ref"]) for r in await list_sources(conn)}
        for system, ref in existing - set(keys):
            await conn.execute("DELETE FROM sources WHERE system = %s AND ref = %s", (system, ref))
        for x in sources:
            await conn.execute(
                "INSERT INTO sources (system, ref, name, url, role, notes) VALUES (%s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (system, ref) DO UPDATE SET name = excluded.name, url = excluded.url,"
                " role = excluded.role, notes = excluded.notes",
                (x.system, x.ref, x.name, x.url, x.role, x.notes),
            )
    return await list_sources(conn)


async def sync_state(conn: psycopg.AsyncConnection) -> dict[str, dict]:
    rows = await (await conn.execute("SELECT key, checked_until, updated_at, updated_by FROM sync_state")).fetchall()
    return {r["key"]: r for r in rows}


async def mark_checked(
    conn: psycopg.AsyncConnection, key: str, checked_until: datetime, actor: str
) -> dict:
    """Record that ``key`` ('slack:C123') has been read up to ``checked_until``.
    The caller checks the key is an allowed source."""
    cur = await conn.execute(
        "INSERT INTO sync_state (key, checked_until, updated_by) VALUES (%s, %s, %s)"
        " ON CONFLICT (key) DO UPDATE SET checked_until = excluded.checked_until, updated_at = now(),"
        " updated_by = excluded.updated_by RETURNING key, checked_until, updated_at, updated_by",
        (key, checked_until, actor),
    )
    return await cur.fetchone()


# -- proposals (profile changes waiting for approval) -------------------------

PROPOSAL_COLUMNS = (
    "id, section, base_content, proposed_content, change_summary, reason, evidence_url, source_keys,"
    " status, proposed_by, proposed_at, review_channel, review_ts, decided_by, decided_via, decided_at,"
    " decision_note, applied_content"
)


async def expire_proposals(conn: psycopg.AsyncConnection, days: int) -> int:
    cur = await conn.execute(
        "UPDATE proposals SET status = 'expired', decided_at = now(),"
        " decision_note = 'No decision within ' || %s || ' days'"
        " WHERE status = 'pending' AND proposed_at < now() - make_interval(days => %s) RETURNING id",
        (days, days),
    )
    return len(await cur.fetchall())


async def propose_change(conn: psycopg.AsyncConnection, p: ProposalIn, actor: str) -> tuple[dict, list[int]]:
    """Record a proposed new text for a section. Returns (proposal, ids it replaced).

    There is at most one pending proposal per section: a new one replaces it, so
    it should include anything still wanted from the old one. A proposal citing a
    source that was rejected before is refused, so a rejected change doesn't keep
    coming back.
    """
    async with conn.transaction():
        if p.source_keys:
            cur = await conn.execute(
                "SELECT id FROM proposals WHERE status = 'rejected' AND source_keys && %s LIMIT 1",
                (p.source_keys,),
            )
            hit = await cur.fetchone()
            if hit:
                raise Conflict(
                    f"A change from that source was rejected before (proposal P{hit['id']}); not proposing it again."
                )
        cur = await conn.execute(
            "SELECT content FROM profile_sections WHERE key = %s FOR UPDATE", (p.section,)
        )
        row = await cur.fetchone()
        current = row["content"] if row else ""
        if p.proposed_content.strip() == current.strip():
            raise ValueError("That's what the section already says; nothing to propose.")
        cur = await conn.execute(
            "SELECT id FROM proposals WHERE section = %s AND status = 'pending' FOR UPDATE", (p.section,)
        )
        replaced = [r["id"] for r in await cur.fetchall()]
        cur = await conn.execute(
            f"INSERT INTO proposals (section, base_content, proposed_content, change_summary, reason,"
            f" evidence_url, source_keys, proposed_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
            f" RETURNING {PROPOSAL_COLUMNS}",
            (p.section, current, p.proposed_content.strip(), p.change_summary, p.reason, p.evidence_url,
             p.source_keys, actor),
        )
        new = await cur.fetchone()
        if replaced:
            await conn.execute(
                "UPDATE proposals SET status = 'superseded', decided_at = now(),"
                " decision_note = 'Replaced by P' || %s WHERE id = ANY(%s)",
                (new["id"], replaced),
            )
        return new, replaced


async def list_proposals(
    conn: psycopg.AsyncConnection, status: str | None = "pending", limit: int = 50
) -> list[dict]:
    query = f"SELECT {PROPOSAL_COLUMNS} FROM proposals"
    params: list[Any] = []
    if status:
        query += " WHERE status = %s"
        params.append(status)
    query += " ORDER BY id DESC LIMIT %s"
    params.append(max(1, min(limit, 200)))
    return await (await conn.execute(query, params)).fetchall()


async def get_proposal(conn: psycopg.AsyncConnection, proposal_id: int) -> dict:
    cur = await conn.execute(f"SELECT {PROPOSAL_COLUMNS} FROM proposals WHERE id = %s", (proposal_id,))
    row = await cur.fetchone()
    if row is None:
        raise NotFound(f"No proposal P{proposal_id}")
    return row


async def attach_review(conn: psycopg.AsyncConnection, proposal_id: int, channel: str, ts: str) -> dict:
    cur = await conn.execute(
        f"UPDATE proposals SET review_channel = %s, review_ts = %s WHERE id = %s RETURNING {PROPOSAL_COLUMNS}",
        (channel, ts, proposal_id),
    )
    row = await cur.fetchone()
    if row is None:
        raise NotFound(f"No proposal P{proposal_id}")
    return row


async def decide_proposal(
    conn: psycopg.AsyncConnection,
    proposal_id: int,
    d: DecisionIn,
    token_label: str,
    approvers: list[dict],
) -> dict:
    """Approve (apply to the profile) or reject a pending proposal.

    ``decided_by`` must be one of the configured approvers when any are set.
    Approval is refused if the section changed after the proposal was drafted;
    it then needs re-proposing against the current text.
    """
    name = match_approver(approvers, d.decided_by) if approvers else d.decided_by
    if name is None:
        allowed = ", ".join(a["name"] for a in approvers)
        raise Forbidden(f"{d.decided_by!r} isn't an approver. Approvers: {allowed}.")
    via = f"{d.via} (token: {token_label})"
    async with conn.transaction():
        cur = await conn.execute(f"SELECT {PROPOSAL_COLUMNS} FROM proposals WHERE id = %s FOR UPDATE", (proposal_id,))
        p = await cur.fetchone()
        if p is None:
            raise NotFound(f"No proposal P{proposal_id}")
        if p["status"] != "pending":
            raise Conflict(f"P{proposal_id} is already {p['status']}")
        if d.decision == "reject":
            cur = await conn.execute(
                f"UPDATE proposals SET status = 'rejected', decided_by = %s, decided_via = %s, decided_at = now(),"
                f" decision_note = %s WHERE id = %s RETURNING {PROPOSAL_COLUMNS}",
                (name, via, d.note, proposal_id),
            )
            return await cur.fetchone()
        cur = await conn.execute("SELECT content FROM profile_sections WHERE key = %s FOR UPDATE", (p["section"],))
        row = await cur.fetchone()
        current = row["content"] if row else ""
        if current.strip() != p["base_content"].strip():
            raise Conflict(
                f"'{PROFILE_SECTIONS[p['section']]}' changed after P{proposal_id} was proposed;"
                " propose it again against the current text."
            )
        content = (d.corrected_content or p["proposed_content"]).strip()
        await set_profile_section(conn, p["section"], content, f"{name} (approved P{proposal_id})")
        cur = await conn.execute(
            f"UPDATE proposals SET status = 'approved', decided_by = %s, decided_via = %s, decided_at = now(),"
            f" decision_note = %s, applied_content = %s WHERE id = %s RETURNING {PROPOSAL_COLUMNS}",
            (name, via, d.note, content, proposal_id),
        )
        return await cur.fetchone()


def jsonable(row: dict) -> dict:
    """Make a row safe for JSON (datetimes to ISO strings)."""
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return json.loads(json.dumps(out, default=str))
