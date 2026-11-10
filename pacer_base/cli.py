"""pacer-base command line.

Server side (needs DATABASE_URL):
    pacer-base serve
    pacer-base migrate

Admin from anywhere (talks to the API; set PACER_BASE_URL and PACER_BASE_TOKEN):
    pacer-base setup pacer-setup.json         settings, clients and sources in one go
    pacer-base add-client "Acme" --alias AC --domain acme.com
    pacer-base clients
    pacer-base sources acme
    pacer-base set-sources acme sources.json
    pacer-base settings
    pacer-base set-setting daily_entry_cap 40
    pacer-base proposals acme
    pacer-base add-token "Jo Bloggs" --scope approve
    pacer-base tokens
    pacer-base revoke-token 3
    pacer-base context acme
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


# -- server-side commands ---------------------------------------------------
def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app

    port = int(os.environ.get("PORT", args.port))
    # proxy_headers: Railway terminates TLS in front of the service.
    uvicorn.run(create_app(), host=args.host, port=port, proxy_headers=True, forwarded_allow_ips="*")
    return 0


def cmd_migrate(_: argparse.Namespace) -> int:
    from .config import Settings
    from .db import Database

    async def run() -> None:
        db = Database(Settings.from_env())
        await db.open()  # applies control and every client's migrations
        await db.close()

    asyncio.run(run())
    print("Migrations applied.")
    return 0


# -- API client -------------------------------------------------------------
class Api:
    def __init__(self, url: str, token: str):
        if not url or not token:
            raise SystemExit("Set PACER_BASE_URL and PACER_BASE_TOKEN (or pass --url and --token).")
        self.url = url.rstrip("/")
        self.token = token

    def __call__(self, method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.url + path,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, raw.decode(errors="replace")

    def ok(self, method: str, path: str, body: dict | None = None) -> Any:
        status, data = self(method, path, body)
        if status >= 400:
            detail = data.get("detail", data) if isinstance(data, dict) else data
            raise SystemExit(f"Error {status}: {detail}")
        return data


def _api(args: argparse.Namespace) -> Api:
    return Api(args.url or os.environ.get("PACER_BASE_URL", ""), args.token or os.environ.get("PACER_BASE_TOKEN", ""))


def cmd_clients(args: argparse.Namespace) -> int:
    for c in _api(args).ok("GET", "/api/clients"):
        s = c["stats"]
        print(f"{c['slug']:<24} {c['name']:<30} {c['status']:<8} {s['entries']:>5} entries  last {s['last_updated'] or '-'}")
    return 0


def cmd_add_client(args: argparse.Namespace) -> int:
    body = {"name": args.name, "aliases": args.alias or [], "domains": args.domain or []}
    if args.slug:
        body["slug"] = args.slug
    c = _api(args).ok("POST", "/api/clients", body)
    print(f"Created {c['name']} as '{c['slug']}' (database {c['db_name']}).")
    return 0


def cmd_update_client(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {}
    if args.name:
        body["name"] = args.name
    if args.alias is not None:
        body["aliases"] = args.alias
    if args.domain is not None:
        body["domains"] = args.domain
    if args.status:
        body["status"] = args.status
    if not body:
        raise SystemExit("Nothing to change.")
    c = _api(args).ok("PATCH", f"/api/clients/{urllib.parse.quote(args.client)}", body)
    print(json.dumps(c, indent=2))
    return 0


def cmd_add_token(args: argparse.Namespace) -> int:
    api = _api(args)
    t = api.ok("POST", "/api/tokens", {"label": args.label, "scope": args.scope, "clients": args.client or None})
    limited = ", ".join(t["clients"]) if t["clients"] else "all clients"
    print(f"Token #{t['id']} '{t['label']}' ({t['scope']}, {limited}). It is shown once:\n")
    print(f"  {t['token']}\n")
    print("claude.ai connector URL (Settings > Connectors > Add custom connector):")
    print(f"  {api.url}/mcp/{t['token']}\n")
    print("Claude Code:")
    print(f'  claude mcp add --transport http pacer-base {api.url}/mcp --header "Authorization: Bearer {t["token"]}"')
    return 0


def _load_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Can't read {path}: {exc}")


def cmd_sources(args: argparse.Namespace) -> int:
    rows = _api(args).ok("GET", f"/api/clients/{urllib.parse.quote(args.client)}/sources")
    if not rows:
        print("No sources yet.")
    for x in rows:
        print(f"{x['system']:<12} {x['ref']:<40} {x['name']:<32} {x['role']}")
    return 0


def cmd_set_sources(args: argparse.Namespace) -> int:
    data = _load_json(args.file)
    body = data if isinstance(data, dict) and "sources" in data else {"sources": data}
    rows = _api(args).ok("PUT", f"/api/clients/{urllib.parse.quote(args.client)}/sources", body)
    print(f"{len(rows)} source(s) set for {args.client}.")
    return 0


def cmd_settings(args: argparse.Namespace) -> int:
    print(json.dumps(_api(args).ok("GET", "/api/settings"), indent=2))
    return 0


def cmd_set_setting(args: argparse.Namespace) -> int:
    try:
        value = json.loads(args.value)
    except ValueError:
        value = args.value
    print(json.dumps(_api(args).ok("PUT", f"/api/settings/{args.key}", {"value": value}), indent=2))
    return 0


def cmd_proposals(args: argparse.Namespace) -> int:
    rows = _api(args).ok("GET", f"/api/clients/{urllib.parse.quote(args.client)}/proposals?status={args.status}")
    if not rows:
        print(f"No {args.status} proposals.")
    for p in rows:
        who = f" by {p['decided_by']}" if p["decided_by"] else ""
        print(f"P{p['id']:<4} {p['status']:<10} {p['section']:<14} {p['change_summary']}{who}")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Apply a setup file: agency settings, then each client and its sources. Safe to re-run."""
    api = _api(args)
    data = _load_json(args.file)
    for key, value in (data.get("settings") or {}).items():
        api.ok("PUT", f"/api/settings/{key}", {"value": value})
        print(f"setting {key}: set")
    for c in data.get("clients") or []:
        body = {k: c[k] for k in ("name", "slug", "aliases", "domains") if k in c}
        status, resp = api("POST", "/api/clients", body)
        if status == 201:
            print(f"client {resp['slug']}: created (database {resp['db_name']})")
            slug = resp["slug"]
        elif status == 409:
            slug = c.get("slug") or c["name"]
            api.ok("PATCH", f"/api/clients/{urllib.parse.quote(slug)}",
                   {k: c[k] for k in ("name", "aliases", "domains") if k in c})
            print(f"client {slug}: already there, details updated")
        else:
            raise SystemExit(f"Error {status} creating {c.get('name')}: {resp}")
        if "sources" in c:
            rows = api.ok("PUT", f"/api/clients/{urllib.parse.quote(slug)}/sources", {"sources": c["sources"]})
            print(f"client {slug}: {len(rows)} source(s) set")
    return 0


def cmd_tokens(args: argparse.Namespace) -> int:
    for t in _api(args).ok("GET", "/api/tokens"):
        state = "revoked" if t["revoked_at"] else f"last used {t['last_used_at'] or 'never'}"
        limited = ",".join(t["clients"]) if t["clients"] else "all"
        print(f"#{t['id']:<4} {t['label']:<30} {t['scope']:<6} clients={limited:<20} {state}")
    return 0


def cmd_revoke_token(args: argparse.Namespace) -> int:
    _api(args).ok("DELETE", f"/api/tokens/{args.id}")
    print(f"Revoked token #{args.id}.")
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    data = _api(args).ok("GET", f"/api/clients/{urllib.parse.quote(args.client)}/context")
    print(data["markdown"])
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pacer-base", description="Pacer Base: per-client context for Claude.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="Run the web service (Railway start command)")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)
    sub.add_parser("migrate", help="Apply database migrations").set_defaults(fn=cmd_migrate)

    def api_parser(name: str, help: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help)
        sp.add_argument("--url", help="Pacer Base URL (default: $PACER_BASE_URL)")
        sp.add_argument("--token", help="API token (default: $PACER_BASE_TOKEN)")
        return sp

    api_parser("clients", "List clients").set_defaults(fn=cmd_clients)

    s = api_parser("add-client", "Create a client and its database (admin)")
    s.add_argument("name")
    s.add_argument("--slug", help="Id to use (default: from the name, e.g. 'S&S' -> s-and-s)")
    s.add_argument("--alias", action="append", help="Other names used for the client (repeatable)")
    s.add_argument("--domain", action="append", help="Client email domain, used to find their meetings (repeatable)")
    s.set_defaults(fn=cmd_add_client)

    s = api_parser("update-client", "Change a client's name, aliases, domains or status (admin)")
    s.add_argument("client")
    s.add_argument("--name")
    s.add_argument("--alias", action="append", help="Replaces the alias list (repeatable)")
    s.add_argument("--domain", action="append", help="Replaces the domain list (repeatable)")
    s.add_argument("--status", choices=["active", "paused", "archived"])
    s.set_defaults(fn=cmd_update_client)

    s = api_parser("add-token", "Create an API token (admin)")
    s.add_argument("label", help="Who or what it's for, e.g. 'Team connector'")
    s.add_argument(
        "--scope",
        choices=["read", "write", "approve", "admin"],
        default="write",
        help="read; write (log entries, propose changes); approve (also approve changes); admin",
    )
    s.add_argument("--client", action="append", help="Limit to this client (repeatable). Default: all clients")
    s.set_defaults(fn=cmd_add_token)

    api_parser("tokens", "List tokens (admin)").set_defaults(fn=cmd_tokens)
    s = api_parser("revoke-token", "Revoke a token (admin)")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_revoke_token)

    s = api_parser("setup", "Apply a setup file: settings, clients and their sources (admin)")
    s.add_argument("file")
    s.set_defaults(fn=cmd_setup)

    s = api_parser("sources", "List the places the round-up may read for a client")
    s.add_argument("client")
    s.set_defaults(fn=cmd_sources)

    s = api_parser("set-sources", "Replace a client's sources from a JSON file (admin)")
    s.add_argument("client")
    s.add_argument("file")
    s.set_defaults(fn=cmd_set_sources)

    api_parser("settings", "Show agency-wide settings").set_defaults(fn=cmd_settings)
    s = api_parser("set-setting", "Change one setting (admin); the value is JSON")
    s.add_argument("key", help="approvers, review_channel, agency_channels, daily_entry_cap, proposal_expiry_days")
    s.add_argument("value")
    s.set_defaults(fn=cmd_set_setting)

    s = api_parser("proposals", "List a client's profile changes")
    s.add_argument("client")
    s.add_argument("--status", default="pending", choices=["pending", "approved", "rejected", "expired", "superseded", "all"])
    s.set_defaults(fn=cmd_proposals)

    s = api_parser("context", "Print a client's context as Claude sees it")
    s.add_argument("client")
    s.set_defaults(fn=cmd_context)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
