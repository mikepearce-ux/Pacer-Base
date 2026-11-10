"""The web service: REST API for tools and automations, MCP endpoint for Claude.

    GET  /health                                  no auth
    /mcp  or  /mcp/<token>                        MCP (Streamable HTTP)
    /api/...                                      REST, Authorization: Bearer <token>
"""


import contextlib
import logging
from datetime import datetime
from typing import Annotated, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import __version__, render, store
from .auth import Authenticator, MCPAuth, Principal, bearer
from .config import Settings
from .db import Database
from .mcp_server import build_mcp
from .models import PROFILE_SECTIONS, ClientIn, ClientUpdate, EntryIn, ProfileUpdate, TokenIn

log = logging.getLogger("pacer_base")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if not settings.admin_token:
        log.warning("PACER_BASE_ADMIN_TOKEN is not set: no one can create clients or tokens.")
    elif len(settings.admin_token) < 24:
        log.warning("PACER_BASE_ADMIN_TOKEN is short; use a long random string.")
    db = Database(settings)
    authenticator = Authenticator(db)
    mcp = build_mcp(db)
    # Stateless + JSON responses: every MCP request stands alone, so redeploys
    # and restarts never strand a session, and no sticky routing is needed.
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, host="0.0.0.0")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await db.open()
        async with mcp.session_manager.run():
            yield
        await db.close()

    app = FastAPI(title="Pacer Base", version=__version__, lifespan=lifespan, docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.state.db = db
    app.state.auth = authenticator

    # -- auth helpers -----------------------------------------------------
    async def principal(request: Request) -> Principal:
        who = await authenticator.verify(bearer(request.scope.get("headers", [])))
        if who is None:
            raise HTTPException(401, "Missing or invalid Pacer Base token", headers={"WWW-Authenticate": "Bearer"})
        return who

    def need(scope: str):
        async def dep(who: Annotated[Principal, Depends(principal)]) -> Principal:
            if not who.has(scope):
                raise HTTPException(403, f"This token is {who.scope}-only; {scope} access is needed")
            return who

        return dep

    Reader = Annotated[Principal, Depends(need("read"))]
    Writer = Annotated[Principal, Depends(need("write"))]
    Admin = Annotated[Principal, Depends(need("admin"))]

    async def client_or_404(ref: str, who: Principal, write: bool = False, include_archived: bool = False) -> dict:
        try:
            c = await store.resolve_client(db, ref, who.allowed, include_archived=include_archived)
        except store.NotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        if write and c["status"] == "paused":
            raise HTTPException(409, f"{c['name']} is paused")
        return c

    @app.exception_handler(ValidationError)
    async def _validation(_: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse({"detail": exc.errors(include_url=False, include_context=False)}, status_code=422)

    # -- health -----------------------------------------------------------
    @app.get("/health", include_in_schema=False)
    async def health() -> dict:
        async with db.control.connection() as conn:
            await conn.execute("SELECT 1")
        return {"ok": True, "version": __version__}

    @app.get("/", include_in_schema=False)
    async def root() -> dict:
        return {"service": "Pacer Base", "mcp": "/mcp", "api_docs": "/api/docs"}

    # -- clients ----------------------------------------------------------
    @app.get("/api/clients")
    async def list_clients(who: Reader) -> list[dict]:
        out = []
        for c in await store.list_clients(db, who.allowed):
            async with db.client(c["db_name"]) as conn:
                s = await store.stats(conn)
            row = store.jsonable({k: v for k, v in c.items() if k != "db_name"})
            row["stats"] = store.jsonable(s)
            out.append(row)
        return out

    @app.post("/api/clients", status_code=201)
    async def create_client(body: ClientIn, _: Admin) -> dict:
        try:
            row = await store.create_client(db, body)
        except store.Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return store.jsonable(row)

    @app.patch("/api/clients/{client}")
    async def update_client(client: str, body: ClientUpdate, who: Admin) -> dict:
        c = await client_or_404(client, who, include_archived=True)
        row = await store.update_client(db, c["slug"], **body.model_dump(exclude_none=True))
        if c["status"] == "archived" and row["status"] != "archived":
            # Restored: catch up on any migrations added while it was archived.
            await db.ensure_client_database(row["db_name"])
        return store.jsonable(row)

    @app.get("/api/clients/{client}/context")
    async def context(client: str, who: Reader, recent: Annotated[int, Query(ge=0, le=50)] = 12) -> dict:
        c = await client_or_404(client, who)
        async with db.client(c["db_name"]) as conn:
            profile = await store.get_profile(conn)
            entries = await store.list_entries(conn, limit=recent) if recent else []
            s = await store.stats(conn)
        return {
            "client": store.jsonable({k: v for k, v in c.items() if k != "db_name"}),
            "markdown": render.client_context(c, profile, entries, s),
            "profile": {k: store.jsonable(v) for k, v in profile.items()},
            "recent": [store.jsonable(e) for e in entries],
            "stats": store.jsonable(s),
        }

    # -- profile ----------------------------------------------------------
    @app.get("/api/clients/{client}/profile")
    async def get_profile(client: str, who: Reader) -> dict:
        c = await client_or_404(client, who)
        async with db.client(c["db_name"]) as conn:
            profile = await store.get_profile(conn)
        return {k: store.jsonable(profile[k]) if k in profile else None for k in PROFILE_SECTIONS}

    @app.put("/api/clients/{client}/profile/{section}")
    async def put_profile(client: str, section: str, body: ProfileUpdate, who: Writer) -> dict:
        c = await client_or_404(client, who, write=True)
        try:
            async with db.client(c["db_name"]) as conn:
                row = await store.set_profile_section(conn, section, body.content, who.label)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return store.jsonable(row)

    # -- entries ----------------------------------------------------------
    @app.get("/api/clients/{client}/entries")
    async def list_entries(
        client: str,
        who: Reader,
        q: str | None = None,
        kind: Annotated[list[str] | None, Query()] = None,
        since: datetime | None = None,
        until: datetime | None = None,
        tag: str | None = None,
        status: str | None = None,
        include_archived: bool = False,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict]:
        c = await client_or_404(client, who)
        try:
            async with db.client(c["db_name"]) as conn:
                if q:
                    rows = await store.search_entries(conn, q, kinds=kind, since=since, limit=limit)
                else:
                    rows = await store.list_entries(
                        conn, kinds=kind, since=since, until=until, tag=tag, status=status,
                        include_archived=include_archived, limit=limit, offset=offset,
                    )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return [store.jsonable(r) for r in rows]

    @app.post("/api/clients/{client}/entries")
    async def save_entry(client: str, body: EntryIn, who: Writer) -> JSONResponse:
        """Create an entry, or update the one with the same source + external_id
        (or the same url and kind). 201 when created, 200 when updated."""
        c = await client_or_404(client, who, write=True)
        try:
            async with db.client(c["db_name"]) as conn:
                row, action = await store.save_entry(conn, body, who.label)
        except (ValueError, store.Conflict) as exc:
            raise HTTPException(422, str(exc)) from exc
        return JSONResponse({"action": action, "entry": store.jsonable(row)}, status_code=201 if action == "created" else 200)

    @app.get("/api/clients/{client}/entries/{entry_id}")
    async def get_entry(client: str, entry_id: int, who: Reader) -> dict:
        c = await client_or_404(client, who)
        try:
            async with db.client(c["db_name"]) as conn:
                return store.jsonable(await store.get_entry(conn, entry_id))
        except store.NotFound as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.patch("/api/clients/{client}/entries/{entry_id}")
    async def patch_entry(client: str, entry_id: int, body: EntryIn, who: Writer) -> dict:
        c = await client_or_404(client, who, write=True)
        try:
            async with db.client(c["db_name"]) as conn:
                row, _ = await store.save_entry(conn, body, who.label, entry_id=entry_id)
        except store.NotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except (ValueError, store.Conflict) as exc:
            raise HTTPException(422, str(exc)) from exc
        return store.jsonable(row)

    @app.delete("/api/clients/{client}/entries/{entry_id}")
    async def archive(client: str, entry_id: int, who: Writer) -> dict:
        c = await client_or_404(client, who, write=True)
        try:
            async with db.client(c["db_name"]) as conn:
                return store.jsonable(await store.archive_entry(conn, entry_id, who.label))
        except store.NotFound as exc:
            raise HTTPException(404, str(exc)) from exc

    # -- tokens -----------------------------------------------------------
    @app.get("/api/tokens")
    async def list_tokens(_: Admin) -> list[dict]:
        return [store.jsonable(r) for r in await authenticator.list()]

    @app.post("/api/tokens", status_code=201)
    async def create_token(body: TokenIn, _: Admin) -> dict:
        if body.clients is not None:
            known = {c["slug"] for c in await store.list_clients(db, include_archived=True)}
            unknown = [s for s in body.clients if s not in known]
            if unknown:
                raise HTTPException(422, f"Unknown client(s): {', '.join(unknown)}")
        token, row = await authenticator.create(body.label, body.scope, body.clients)
        return {"token": token, "note": "Shown once. Store it in a password manager.", **store.jsonable(row)}

    @app.delete("/api/tokens/{token_id}")
    async def revoke_token(token_id: int, _: Admin) -> dict:
        if not await authenticator.revoke(token_id):
            raise HTTPException(404, "No active token with that id")
        return {"revoked": token_id}

    # MCP last: it's mounted at the root, so the routes above take precedence.
    app.mount("/", MCPAuth(mcp_app, authenticator))
    return app


def app() -> FastAPI:  # for `uvicorn --factory pacer_base.app:app`
    return create_app()
