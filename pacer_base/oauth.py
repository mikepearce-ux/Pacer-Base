"""An automatic OAuth handshake, for connector clients that insist on one.

Pacer Base has no sign-in: access is the key in the connector URL
(``/mcp/<key>``). Some clients (claude.ai's custom connectors, as of late 2026)
still run OAuth discovery and dynamic client registration when you press
Connect, and fail with "Couldn't register with ...'s sign-in service" when a
server has none. So Pacer Base answers that handshake itself, with no login
page: the client registers, is sent straight back with a code, and swaps it
for a token.

That token grants nothing on its own. Every MCP request is still authorised by
the key in the URL, and a handshake is only offered for a URL whose key is
valid, so there's no way in without the key.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .auth import Authenticator, hash_token
from .db import Database

CODE_SECONDS = 600
ACCESS_SECONDS = 30 * 24 * 3600
# Connector endpoints that take a key in the URL: Pacer Base and Attention Lab.
MCP_PREFIXES = ("/heatmap/mcp/", "/mcp/")


def _base(request: Request) -> str:
    override = request.app.state.public_url
    if override:
        return override.rstrip("/")
    return f"{request.url.scheme}://{request.url.netloc}"


def _error(status: int, error: str, description: str) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status)


def _redirect_ok(uri: str) -> bool:
    p = urlparse(uri)
    if p.scheme == "https" and p.netloc:
        return True
    # Desktop and CLI clients receive the code on a local port.
    return p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1", "::1")


def _pkce_ok(verifier: str, challenge: str) -> bool:
    digest = hashlib.sha256(verifier.encode()).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return secrets.compare_digest(expected, challenge)


def build_router(db: Database, authenticator: Authenticator) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    async def key_from_resource(base: str, resource: str | None) -> str | None:
        """The connector key inside a resource URL, if it's a valid one."""
        if not resource:
            return None
        for prefix in MCP_PREFIXES:
            if resource.startswith(base + prefix):
                key = resource[len(base + prefix):].strip("/")
                if not key or "/" in key:
                    return None
                return key if await authenticator.verify(key) else None
        return None

    # -- discovery ----------------------------------------------------------
    async def describe(request: Request, prefix: str, key: str, name: str):
        base = _base(request)
        if not await authenticator.verify(key):
            return _error(404, "not_found", "Not found")
        return {
            "resource": f"{base}{prefix}{key}",
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
            "resource_name": name,
        }

    @router.get("/.well-known/oauth-protected-resource/mcp/{key}")
    async def protected_resource(key: str, request: Request):
        return await describe(request, "/mcp/", key, "Pacer Base")

    @router.get("/.well-known/oauth-protected-resource/heatmap/mcp/{key}")
    async def protected_resource_heatmap(key: str, request: Request):
        return await describe(request, "/heatmap/mcp/", key, "Attention Lab")

    @router.get("/.well-known/oauth-authorization-server")
    async def authorization_server(request: Request):
        base = _base(request)
        return {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
        }

    # -- registration (RFC 7591) ----------------------------------------------
    async def register_client(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return _error(400, "invalid_client_metadata", "Body must be JSON")
        uris = body.get("redirect_uris") or []
        if not isinstance(uris, list) or not uris or not all(isinstance(u, str) and _redirect_ok(u) for u in uris):
            return _error(400, "invalid_redirect_uri", "redirect_uris must be https (or http on localhost)")
        method = body.get("token_endpoint_auth_method") or "none"
        if method not in ("none", "client_secret_post", "client_secret_basic"):
            method = "none"
        client_id = "pbc_" + secrets.token_urlsafe(16)
        secret = "pbs_" + secrets.token_urlsafe(32) if method != "none" else None
        name = str(body.get("client_name") or "")[:120]
        async with db.control.connection() as conn:
            await conn.execute(
                "INSERT INTO oauth_clients (client_id, client_secret_hash, client_name, redirect_uris, auth_method)"
                " VALUES (%s, %s, %s, %s, %s)",
                (client_id, hash_token(secret) if secret else None, name, uris, method),
            )
        out = {
            "client_id": client_id,
            "client_id_issued_at": int(time.time()),
            "client_name": name,
            "redirect_uris": uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": method,
        }
        if secret:
            out["client_secret"] = secret
            out["client_secret_expires_at"] = 0
        return JSONResponse(out, status_code=201)

    router.add_api_route("/oauth/register", register_client, methods=["POST"])
    # Clients that find no metadata fall back to /register at the root.
    router.add_api_route("/register", register_client, methods=["POST"])

    async def get_client(client_id: str) -> dict | None:
        async with db.control.connection() as conn:
            cur = await conn.execute("SELECT * FROM oauth_clients WHERE client_id = %s", (client_id,))
            return await cur.fetchone()

    # -- authorize: approves straight away, no login page --------------------
    async def authorize(request: Request):
        q = request.query_params
        client = await get_client(q.get("client_id", ""))
        redirect_uri = q.get("redirect_uri", "")
        if client is None:
            return _error(400, "invalid_client", "Unknown client_id; register first")
        if not redirect_uri and len(client["redirect_uris"]) == 1:
            redirect_uri = client["redirect_uris"][0]
        if redirect_uri not in client["redirect_uris"]:
            return _error(400, "invalid_request", "redirect_uri doesn't match the registered ones")

        def back(**params: str) -> RedirectResponse:
            if q.get("state"):
                params["state"] = q["state"]
            sep = "&" if "?" in redirect_uri else "?"
            return RedirectResponse(redirect_uri + sep + urlencode(params), status_code=302)

        if q.get("response_type") != "code":
            return back(error="unsupported_response_type", error_description="Only response_type=code")
        challenge = q.get("code_challenge", "")
        if not challenge or q.get("code_challenge_method", "S256") != "S256":
            return back(error="invalid_request", error_description="PKCE with S256 is required")
        resource = q.get("resource")
        if resource and not await key_from_resource(_base(request), resource):
            return back(error="invalid_target", error_description="That connector URL has no valid key")
        code = secrets.token_urlsafe(32)
        async with db.control.connection() as conn:
            await conn.execute("DELETE FROM oauth_codes WHERE expires_at < now() - interval '1 day'")
            await conn.execute(
                "INSERT INTO oauth_codes (code_hash, client_id, redirect_uri, code_challenge, resource, scope, expires_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s))",
                (hash_token(code), client["client_id"], redirect_uri, challenge, resource, q.get("scope"), CODE_SECONDS),
            )
        return back(code=code)

    router.add_api_route("/oauth/authorize", authorize, methods=["GET"])
    router.add_api_route("/authorize", authorize, methods=["GET"])

    # -- token ------------------------------------------------------------
    async def token(
        request: Request,
        grant_type: str = Form(""),
        code: str = Form(""),
        redirect_uri: str = Form(""),
        code_verifier: str = Form(""),
        refresh_token: str = Form(""),
        client_id: str = Form(""),
        client_secret: str = Form(""),
        resource: str = Form(""),
    ):
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                cid, _, csecret = base64.b64decode(header[6:]).decode().partition(":")
                client_id, client_secret = client_id or cid, client_secret or csecret
            except ValueError:
                return _error(401, "invalid_client", "Bad basic auth")
        client = await get_client(client_id)
        if client is None:
            return _error(401, "invalid_client", "Unknown client_id")
        if client["auth_method"] != "none":
            if not client_secret or not secrets.compare_digest(hash_token(client_secret), client["client_secret_hash"] or ""):
                return _error(401, "invalid_client", "Wrong client secret")

        async with db.control.connection() as conn:
            if grant_type == "authorization_code":
                async with conn.transaction():
                    cur = await conn.execute(
                        "SELECT * FROM oauth_codes WHERE code_hash = %s FOR UPDATE", (hash_token(code),)
                    )
                    row = await cur.fetchone()
                    if (
                        row is None
                        or row["used_at"] is not None
                        or row["client_id"] != client["client_id"]
                        or (redirect_uri and redirect_uri != row["redirect_uri"])
                    ):
                        return _error(400, "invalid_grant", "Code is unknown, used or for another client")
                    cur = await conn.execute("SELECT now() > %s AS expired", (row["expires_at"],))
                    if (await cur.fetchone())["expired"]:
                        return _error(400, "invalid_grant", "Code expired")
                    if not code_verifier or not _pkce_ok(code_verifier, row["code_challenge"]):
                        return _error(400, "invalid_grant", "PKCE check failed")
                    await conn.execute("UPDATE oauth_codes SET used_at = now() WHERE code_hash = %s", (row["code_hash"],))
                    res, scope = row["resource"], row["scope"]
            elif grant_type == "refresh_token":
                async with conn.transaction():
                    cur = await conn.execute(
                        "SELECT * FROM oauth_refresh_tokens WHERE token_hash = %s AND revoked_at IS NULL FOR UPDATE",
                        (hash_token(refresh_token),),
                    )
                    row = await cur.fetchone()
                    if row is None or row["client_id"] != client["client_id"]:
                        return _error(400, "invalid_grant", "Unknown refresh token")
                    await conn.execute(
                        "UPDATE oauth_refresh_tokens SET revoked_at = now() WHERE token_hash = %s", (row["token_hash"],)
                    )
                    res, scope = row["resource"], row["scope"]
            else:
                return _error(400, "unsupported_grant_type", "Use authorization_code or refresh_token")

            if res and not await key_from_resource(_base(request), res):
                return _error(400, "invalid_grant", "The connector key for this resource is no longer valid")
            new_refresh = "pbr_" + secrets.token_urlsafe(32)
            await conn.execute(
                "INSERT INTO oauth_refresh_tokens (token_hash, client_id, resource, scope) VALUES (%s, %s, %s, %s)",
                (hash_token(new_refresh), client["client_id"], res, scope),
            )
        out = {
            # Opaque and not checked anywhere: the key in the URL is what grants access.
            "access_token": "pba_" + secrets.token_urlsafe(32),
            "token_type": "Bearer",
            "expires_in": ACCESS_SECONDS,
            "refresh_token": new_refresh,
        }
        if scope:
            out["scope"] = scope
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    router.add_api_route("/oauth/token", token, methods=["POST"])
    router.add_api_route("/token", token, methods=["POST"])
    return router
