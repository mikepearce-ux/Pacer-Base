"""Reads and writes. Registry functions take the Database; content functions take
one client's connection, so they can only ever touch that client's data."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable

import psycopg
from psycopg.types.json import Jsonb

from .db import Database, client_db_name
from .models import KINDS, PROFILE_SECTIONS, ClientIn, EntryIn, slugify


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
# Client content (one client's database)
# --------------------------------------------------------------------------

ENTRY_COLUMNS = (
    "id, kind, title, summary, body, url, status, source, external_id, tags, metadata,"
    " occurred_at, created_at, updated_at, created_by, updated_by, archived_at"
)
LIST_COLUMNS = (
    "id, kind, title, summary, url, status, source, external_id, tags, occurred_at,"
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


async def _find_existing(conn: psycopg.AsyncConnection, e: EntryIn, source: str) -> int | None:
    if e.external_id:
        cur = await conn.execute(
            "SELECT id FROM entries WHERE source = %s AND external_id = %s FOR UPDATE",
            (source, e.external_id),
        )
        row = await cur.fetchone()
        return row["id"] if row else None
    if e.url and e.kind:
        cur = await conn.execute(
            "SELECT id FROM entries WHERE url = %s AND kind = %s AND archived_at IS NULL"
            " ORDER BY id LIMIT 1 FOR UPDATE",
            (e.url, e.kind),
        )
        row = await cur.fetchone()
        return row["id"] if row else None
    return None


async def save_entry(
    conn: psycopg.AsyncConnection,
    e: EntryIn,
    actor: str,
    entry_id: int | None = None,
    default_source: str = "manual",
) -> tuple[dict, str]:
    """Create an entry, or update one. Returns (entry, "created" | "updated").

    Without ``entry_id``, an existing entry is updated instead of duplicated when
    it has the same source + external_id, or (with no external_id) the same url
    and kind. That makes re-running an import or a skill safe. ``default_source``
    is only used for a new entry (and to match external_id); an update never
    changes the source unless one is passed.
    """
    source = e.source or default_source
    async with conn.transaction():
        target = entry_id
        if target is None:
            target = await _find_existing(conn, e, source)
        if target is not None:
            row = await _update_entry(conn, target, e, actor)
            return row, "updated"
        e.require_new()
        cur = await conn.execute(
            f"INSERT INTO entries (kind, title, summary, body, url, status, source, external_id, tags,"
            f" metadata, occurred_at, created_by, updated_by)"
            f" VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, coalesce(%s, now()), %s, %s)"
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
        if name == "metadata":
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


def jsonable(row: dict) -> dict:
    """Make a row safe for JSON (datetimes to ISO strings)."""
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return json.loads(json.dumps(out, default=str))
