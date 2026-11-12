"""API tokens.

Tokens are random strings shown once and stored only as SHA-256 hashes. Each has
a scope and optionally a list of clients it may use; a token limited to one
client can't see that any other client exists. Scopes, each including the last:

* read: read profiles and entries
* write: also log entries and propose profile changes
* approve: also approve or reject proposals and edit profiles directly
* admin: also manage clients, their sources, settings and tokens

The MCP endpoint accepts a token two ways, because claude.ai custom connectors
can't send headers:

* ``Authorization: Bearer <token>`` on ``/mcp`` (Claude Code, scripts)
* in the URL: ``/mcp/<token>`` (claude.ai connector settings)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Iterable

from starlette.types import ASGIApp, Receive, Scope, Send

from .db import Database

SCOPES = ("read", "write", "approve", "admin")
TOKEN_PREFIX = "pb_"
CACHE_SECONDS = 30
PRINCIPAL_KEY = "pacer_base.principal"


@dataclass(frozen=True)
class Principal:
    label: str
    scope: str
    clients: frozenset[str] | None = None  # None: every client
    token_id: int | None = None

    def has(self, needed: str) -> bool:
        return SCOPES.index(self.scope) >= SCOPES.index(needed)

    @property
    def allowed(self) -> frozenset[str] | None:
        return self.clients

    def can_use(self, slug: str) -> bool:
        return self.clients is None or slug in self.clients


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


class Authenticator:
    def __init__(self, db: Database):
        self.db = db
        self._cache: dict[str, tuple[float, Principal | None]] = {}

    async def verify(self, token: str | None) -> Principal | None:
        token = (token or "").strip()
        if not token or len(token) > 200:
            return None
        admin = self.db.settings.admin_token
        if admin and hmac.compare_digest(token.encode(), admin.encode()):
            return Principal(label="admin", scope="admin")
        digest = hash_token(token)
        hit = self._cache.get(digest)
        now = time.monotonic()
        if hit and hit[0] > now:
            return hit[1]
        principal = await self._lookup(digest)
        self._cache[digest] = (now + CACHE_SECONDS, principal)
        if len(self._cache) > 1000:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
        return principal

    async def _lookup(self, digest: str) -> Principal | None:
        async with self.db.control.connection() as conn:
            cur = await conn.execute(
                "UPDATE api_tokens SET last_used_at = now()"
                " WHERE token_hash = %s AND revoked_at IS NULL"
                " RETURNING id, label, scope, clients",
                (digest,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        clients = frozenset(row["clients"]) if row["clients"] is not None else None
        return Principal(label=row["label"], scope=row["scope"], clients=clients, token_id=row["id"])

    def forget(self) -> None:
        self._cache.clear()

    # -- management ---------------------------------------------------------
    async def create(self, label: str, scope: str, clients: Iterable[str] | None) -> tuple[str, dict]:
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
        token = new_token()
        async with self.db.control.connection() as conn:
            cur = await conn.execute(
                "INSERT INTO api_tokens (label, token_hash, scope, clients) VALUES (%s, %s, %s, %s)"
                " RETURNING id, label, scope, clients, created_at",
                (label, hash_token(token), scope, list(clients) if clients is not None else None),
            )
            row = await cur.fetchone()
        return token, row

    async def list(self) -> list[dict]:
        async with self.db.control.connection() as conn:
            cur = await conn.execute(
                "SELECT id, label, scope, clients, created_at, last_used_at, revoked_at"
                " FROM api_tokens ORDER BY id"
            )
            return await cur.fetchall()

    async def revoke(self, token_id: int) -> bool:
        async with self.db.control.connection() as conn:
            cur = await conn.execute(
                "UPDATE api_tokens SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL RETURNING id",
                (token_id,),
            )
            done = await cur.fetchone() is not None
        self.forget()
        return done


def bearer(headers: Iterable[tuple[bytes, bytes]]) -> str | None:
    for name, value in headers:
        if name.lower() == b"authorization":
            kind, _, token = value.decode("latin-1").partition(" ")
            if kind.lower() == "bearer":
                return token.strip()
    return None


class MCPRouter:
    """Sends each connector's paths to its own MCPAuth-wrapped app (longest
    prefix first), and answers 404 for anything else."""

    def __init__(self, routes: list[tuple[str, "MCPAuth"]]):
        self.routes = sorted(routes, key=lambda r: len(r[0]), reverse=True)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path: str = scope["path"]
            for prefix, app in self.routes:
                if path == prefix or path.startswith(prefix + "/"):
                    await app(scope, receive, send)
                    return
            await _json(send, 404, {"error": "Not found"})
            return
        await self.routes[0][1](scope, receive, send)


class MCPAuth:
    """ASGI wrapper in front of the MCP app: checks the token, rewrites
    ``/mcp/<token>`` to ``/mcp``, and hands the Principal to the tools via the
    request scope."""

    def __init__(self, app: ASGIApp, authenticator: Authenticator, path: str = "/mcp"):
        self.app = app
        self.auth = authenticator
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        if path.rstrip("/") == self.path:
            token = bearer(scope.get("headers", []))
        elif path.startswith(self.path + "/"):
            token = path[len(self.path) + 1 :].strip("/")
            if "/" in token:
                token = None
        else:
            await _json(send, 404, {"error": "Not found"})
            return
        principal = await self.auth.verify(token)
        if principal is None:
            await _json(
                send,
                401,
                {"error": "Missing or invalid Pacer Base token"},
                extra=[(b"www-authenticate", b'Bearer realm="pacer-base"')],
            )
            return
        scope = dict(scope)
        scope["path"] = self.path
        scope["raw_path"] = self.path.encode()
        scope[PRINCIPAL_KEY] = principal
        await self.app(scope, receive, send)


async def _json(send: Send, status: int, payload: dict, extra: list[tuple[bytes, bytes]] | None = None) -> None:
    body = json.dumps(payload).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": headers + (extra or [])})
    await send({"type": "http.response.body", "body": body})
