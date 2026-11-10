"""Connections: one pool for the control database, one small pool per client database.

Every client gets its own Postgres database on the same server. A query made
through one client's pool physically cannot read another client's rows, a
client can be exported with ``pg_dump pb_<slug>`` and handed over, and dropping
a client is ``DROP DATABASE``.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import Settings
from .models import SLUG_RE
from .schema import CLIENT_MIGRATIONS, CONTROL_MIGRATIONS

log = logging.getLogger("pacer_base.db")

# Any fixed number works; it only has to be the same for every process.
MIGRATION_LOCK_ID = 7_240_301


async def apply_migrations(conn: psycopg.AsyncConnection, migrations: list[str]) -> int:
    """Apply any migrations this database hasn't had yet. Returns how many ran."""
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,))
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version int PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        cur = await conn.execute("SELECT coalesce(max(version), 0) AS v FROM schema_migrations")
        row = await cur.fetchone()
        current = row["v"] if isinstance(row, dict) else row[0]
        ran = 0
        for version, statement in enumerate(migrations, start=1):
            if version <= current:
                continue
            await conn.execute(statement)
            await conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            ran += 1
        return ran


def client_db_name(settings: Settings, slug: str) -> str:
    if not SLUG_RE.match(slug):
        raise ValueError(f"Invalid client slug: {slug!r}")
    return f"{settings.client_db_prefix}{slug.replace('-', '_')}"


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.control = AsyncConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=5,
            open=False,
            kwargs={"autocommit": True, "row_factory": dict_row},
            name="control",
        )
        self._client_pools: dict[str, AsyncConnectionPool] = {}
        self._lock = asyncio.Lock()

    # -- lifecycle -----------------------------------------------------------
    async def open(self) -> None:
        await self.control.open(wait=True, timeout=30)
        async with self.control.connection() as conn:
            ran = await apply_migrations(conn, CONTROL_MIGRATIONS)
            if ran:
                log.info("control database: applied %d migration(s)", ran)
            cur = await conn.execute("SELECT slug, db_name FROM clients WHERE status <> 'archived'")
            clients = await cur.fetchall()
        # Bring every client database up to the current schema at start-up, so a
        # deploy with a new migration never serves a half-migrated client.
        for c in clients:
            await self.ensure_client_database(c["db_name"])

    async def close(self) -> None:
        for pool in list(self._client_pools.values()):
            await pool.close()
        self._client_pools.clear()
        await self.control.close()

    # -- client databases ---------------------------------------------------
    def _client_url(self, db_name: str) -> str:
        return make_conninfo(self.settings.database_url, dbname=db_name)

    async def ensure_client_database(self, db_name: str) -> None:
        """Create the client's database if needed and apply its migrations."""
        async with self.control.connection() as conn:
            cur = await conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            if await cur.fetchone() is None:
                try:
                    # CREATE DATABASE can't run in a transaction; the pool is autocommit.
                    await conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
                    log.info("created database %s", db_name)
                except psycopg.errors.DuplicateDatabase:
                    pass
        async with self.client(db_name) as conn:
            ran = await apply_migrations(conn, CLIENT_MIGRATIONS)
            if ran:
                log.info("%s: applied %d migration(s)", db_name, ran)

    async def _pool_for(self, db_name: str) -> AsyncConnectionPool:
        pool = self._client_pools.get(db_name)
        if pool is not None:
            return pool
        async with self._lock:
            pool = self._client_pools.get(db_name)
            if pool is None:
                pool = AsyncConnectionPool(
                    self._client_url(db_name),
                    min_size=0,
                    max_size=4,
                    max_idle=300,
                    open=False,
                    kwargs={"autocommit": True, "row_factory": dict_row},
                    name=db_name,
                )
                await pool.open(wait=False)
                self._client_pools[db_name] = pool
            return pool

    @asynccontextmanager
    async def client(self, db_name: str) -> AsyncIterator[psycopg.AsyncConnection]:
        pool = await self._pool_for(db_name)
        async with pool.connection() as conn:
            yield conn

    async def drop_client_pool(self, db_name: str) -> None:
        pool = self._client_pools.pop(db_name, None)
        if pool is not None:
            await pool.close()
