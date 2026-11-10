"""The automatic sign-in handshake, the way claude.ai runs it on Connect."""

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

from conftest import admin_headers, auth, make_token

CALLBACK = "https://claude.ai/api/mcp/auth_callback"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "mcp-protocol-version": "2025-06-18"}


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def register(api, **extra):
    body = {"client_name": "Claude", "redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none", **extra}
    r = api.post("/oauth/register", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def authorize(api, client_id, challenge, resource, state="st-1"):
    return api.get(
        "/oauth/authorize",
        params={
            "response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": state, "resource": resource,
        },
        follow_redirects=False,
    )


def code_from(resp):
    assert resp.status_code == 302, resp.text
    q = parse_qs(urlparse(resp.headers["location"]).query)
    return q


def test_discovery_only_for_valid_keys(api):
    key = make_token(api, "write")
    base = "http://testserver"
    r = api.get(f"/.well-known/oauth-protected-resource/mcp/{key}")
    assert r.status_code == 200
    meta = r.json()
    assert meta["resource"] == f"{base}/mcp/{key}" and meta["authorization_servers"] == [base]
    assert api.get("/.well-known/oauth-protected-resource/mcp/pb_wrong").status_code == 404
    assert api.get("/.well-known/oauth-protected-resource").status_code == 404
    asm = api.get("/.well-known/oauth-authorization-server").json()
    assert asm["issuer"] == base and asm["registration_endpoint"] == f"{base}/oauth/register"
    assert asm["code_challenge_methods_supported"] == ["S256"]


def test_full_handshake_then_mcp_works(api):
    api.post("/api/clients", json={"name": "Acme"}, headers=admin_headers())
    key = make_token(api, "write", label="team")
    resource = f"http://testserver/mcp/{key}"
    client = register(api)
    verifier, challenge = pkce()

    q = code_from(authorize(api, client["client_id"], challenge, resource))
    assert q["state"] == ["st-1"] and "code" in q
    r = api.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": CALLBACK,
        "client_id": client["client_id"], "code_verifier": verifier, "resource": resource,
    })
    assert r.status_code == 200, r.text
    tokens = r.json()
    assert tokens["token_type"] == "Bearer" and tokens["access_token"].startswith("pba_")
    assert r.headers["cache-control"] == "no-store"

    # The code works once only.
    again = api.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": q["code"][0], "client_id": client["client_id"], "code_verifier": verifier,
    })
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"

    # The client then calls MCP with its access token on the keyed URL: the key grants access.
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_clients", "arguments": {}}}
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {tokens['access_token']}"}
    r = api.post(f"/mcp/{key}", json=body, headers=headers)
    assert r.status_code == 200 and "acme" in r.json()["result"]["content"][0]["text"]
    # The access token alone opens nothing.
    assert api.post("/mcp", json=body, headers=headers).status_code == 401

    # Refresh rotates the refresh token.
    r = api.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client["client_id"]})
    assert r.status_code == 200 and r.json()["refresh_token"] != tokens["refresh_token"]
    old = api.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client["client_id"]})
    assert old.status_code == 400


def test_pkce_and_client_checks(api):
    key = make_token(api, "read")
    resource = f"http://testserver/mcp/{key}"
    client = register(api)
    verifier, challenge = pkce()
    q = code_from(authorize(api, client["client_id"], challenge, resource))
    wrong = api.post("/oauth/token", data={"grant_type": "authorization_code", "code": q["code"][0], "client_id": client["client_id"], "code_verifier": "nope"})
    assert wrong.status_code == 400 and "PKCE" in wrong.json()["error_description"]
    other = register(api)
    stolen = api.post("/oauth/token", data={"grant_type": "authorization_code", "code": q["code"][0], "client_id": other["client_id"], "code_verifier": verifier})
    assert stolen.status_code == 400
    assert api.post("/oauth/token", data={"grant_type": "password", "client_id": client["client_id"]}).status_code == 400
    assert api.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": "pbc_unknown"}).status_code == 401


def test_authorize_refuses_bad_requests(api):
    key = make_token(api, "read")
    client = register(api)
    _, challenge = pkce()
    # A connector URL with an invalid key gets no code.
    q = code_from(authorize(api, client["client_id"], challenge, "http://testserver/mcp/pb_wrong"))
    assert q["error"] == ["invalid_target"] and "code" not in q
    # No PKCE, no code.
    r = api.get("/oauth/authorize", params={"response_type": "code", "client_id": client["client_id"], "redirect_uri": CALLBACK}, follow_redirects=False)
    assert parse_qs(urlparse(r.headers["location"]).query)["error"] == ["invalid_request"]
    # Unregistered redirect or client: an error page, never a redirect.
    r = api.get("/oauth/authorize", params={"response_type": "code", "client_id": client["client_id"], "redirect_uri": "https://evil.example/cb", "code_challenge": challenge}, follow_redirects=False)
    assert r.status_code == 400
    r = api.get("/oauth/authorize", params={"client_id": "pbc_nope", "redirect_uri": CALLBACK}, follow_redirects=False)
    assert r.status_code == 400
    assert api.post("/oauth/register", json={"redirect_uris": ["http://evil.example/cb"]}).status_code == 400
    assert api.post("/oauth/register", json={"redirect_uris": ["http://localhost:6274/cb"]}).status_code == 201
    # A resource-less request still works (and the key is still needed to use MCP).
    q = code_from(authorize(api, client["client_id"], challenge, None))
    assert "code" in q
    _ = key


def test_root_fallback_paths_and_confidential_clients(api):
    key = make_token(api, "write")
    resource = f"http://testserver/mcp/{key}"
    r = api.post("/register", json={"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "client_secret_post"})
    assert r.status_code == 201
    client = r.json()
    assert client["client_secret"].startswith("pbs_")
    verifier, challenge = pkce()
    q = code_from(api.get("/authorize", params={
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": CALLBACK,
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": resource,
    }, follow_redirects=False))
    form = {"grant_type": "authorization_code", "code": q["code"][0], "client_id": client["client_id"], "code_verifier": verifier}
    assert api.post("/token", data={**form, "client_secret": "wrong"}).status_code == 401
    basic = base64.b64encode(f"{client['client_id']}:{client['client_secret']}".encode()).decode()
    r = api.post("/token", data={k: v for k, v in form.items() if k != "client_id"}, headers={"Authorization": f"Basic {basic}"})
    assert r.status_code == 200, r.text


def test_revoked_key_stops_refresh(api):
    key = make_token(api, "write", label="to-revoke")
    resource = f"http://testserver/mcp/{key}"
    client = register(api)
    verifier, challenge = pkce()
    q = code_from(authorize(api, client["client_id"], challenge, resource))
    tokens = api.post("/oauth/token", data={"grant_type": "authorization_code", "code": q["code"][0], "client_id": client["client_id"], "code_verifier": verifier}).json()
    tid = [t for t in api.get("/api/tokens", headers=admin_headers()).json() if t["label"] == "to-revoke"][0]["id"]
    api.delete(f"/api/tokens/{tid}", headers=admin_headers())
    r = api.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client["client_id"]})
    assert r.status_code == 400 and "no longer valid" in r.json()["error_description"]
    assert api.post(f"/mcp/{key}", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={**MCP_HEADERS, **auth("x")}).status_code == 401
