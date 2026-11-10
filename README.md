# Pacer Base

Pacer's shared memory for each client. Every client gets **its own Postgres database** holding a profile (who's who, goals, current tests, numbers we know, issues, promises, key links) and a timeline of **entries**: a short summary of each call, test, report, audit, proposal or doc, with a link to the real thing.

Claude reads and writes it through the **Pacer Base connector** (an MCP server), and the **pacer-base skill** tells Claude when to read it and what to log. Tools and automations can use the REST API.

```
 Granola meetings ─┐                          ┌─ pb_s_and_s        (S&S: profile + entries)
 deliverables ─────┼─ Claude + skill ─ MCP ─┐ │
 test updates ─────┘                        ├─ Pacer Base ─┼─ pb_<next client>
 scripts, n8n ──────────────── REST API ────┘  (Railway)   └─ control db: client list, tokens
```

Pilot: **S&S**.

## What's stored

**Profile sections** (one living brief per client): `overview`, `people`, `goals`, `current_tests`, `numbers`, `issues`, `promises`, `links`. Replacing a section keeps the previous text in history.

**Entries**, one per thing that happened or was made:

| Kind | For |
|---|---|
| `call` | A call or meeting with the client: notes, decisions, actions |
| `test` | An A/B or CRO test: hypothesis, setup, status, result |
| `report` | A performance report, results slide or read-out |
| `audit` | A CRO audit, diagnostic or research piece |
| `proposal` | A proposal, roadmap, SOW or pitch |
| `doc` | Any other document worth finding again |
| `decision` | A decision and why it was made |
| `note` | Anything else |

Each entry has a title, summary, optional body, link, status (e.g. `live`, `finished`), tags, the date it happened, where it came from (`source`, e.g. `granola`) and that system's id (`external_id`). Logging the same `source` + `external_id`, or the same link and kind, again **updates** the entry instead of duplicating it, so syncs can be re-run safely. Everything is full-text searchable.

## Deploy on Railway

1. **New project** > **Deploy from GitHub repo** > `mikepearce-ux/Pacer-Base`. Railway reads `railway.json` and builds the `Dockerfile`.
2. In the project, **+ New** > **Database** > **PostgreSQL**.
3. On the Pacer Base service, **Variables**:
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}` (a reference, so the password isn't copied anywhere)
   - `PACER_BASE_ADMIN_TOKEN` = a long random string:
     ```bash
     python3 -c "import secrets; print(secrets.token_urlsafe(32))"
     ```
     Keep it in the team password manager. It can create clients and tokens.
4. **Settings** > **Networking** > **Generate Domain**.
5. Open `https://<your-domain>/health`; it should say `{"ok": true, ...}`.
6. On the Postgres service, turn on backups for its volume.

The service creates its tables on start, and a database for each client when the client is added. The Railway Postgres user can create databases, so nothing else is needed. One small instance is plenty for the pilot.

## Set up the S&S pilot

On your laptop (Python 3.11+):

```bash
pip install "git+https://github.com/mikepearce-ux/Pacer-Base@claude/lucid-planck-v30od9"
```

```bash
export PACER_BASE_URL=https://<your-domain> PACER_BASE_TOKEN=<admin token>
```

Add the client. `--domain` is the client's email domain, used to find their meetings in Granola; `--alias` is any other name people use for them:

```bash
pacer-base add-client "S&S" --domain <their-domain.com> --alias "<full name>"
```

This creates the database `pb_s_and_s`. Then make the token the connector will use:

```bash
pacer-base add-token "Team connector" --scope write --client s-and-s
```

It prints the token once, the claude.ai connector URL and the Claude Code command. Leaving out `--client` gives access to every client; `--scope read` makes a read-only token.

## Connect Claude

**claude.ai:** add a custom connector (Settings > Connectors; on a Team or Enterprise plan an owner adds it for the organisation) with the URL printed above:

```
https://<your-domain>/mcp/<token>
```

claude.ai connectors can't send custom headers, so the token goes in the URL. Treat that URL like a password.

**Claude Code:**

```bash
claude mcp add --transport http pacer-base https://<your-domain>/mcp --header "Authorization: Bearer <token>"
```

**Skill:** add `skill/pacer-base/` as a skill (in claude.ai, upload the folder as a zip; in Claude Code, copy it to `~/.claude/skills/pacer-base/`). The connector also carries short instructions of its own, so it works without the skill, but the skill adds the Granola sync and what a good entry looks like.

Then try:

- "Sync S&S meetings from Granola since 1 September."
- "What do we know about S&S? Anything I should raise on Thursday's call?"
- "Log this results slide to Pacer Base for S&S: <link>."
- "S&S's sitewide CVR for October was 2.3% (GA4). Update their numbers."

## Connector tools

| Tool | Does |
|---|---|
| `list_clients` | The clients this token can use, with entry counts |
| `get_client_context` | Profile plus recent activity, each item with its id and link. Claude calls this first |
| `search_client_context` | Full-text search of one client's entries |
| `list_entries` | Timeline filtered by kind, date, tag or status |
| `get_entry` | One entry in full |
| `log_entry` | Add an entry, or update one (by id, link, or source + external id) |
| `update_profile_section` | Replace one profile section |
| `archive_entry` | Hide an entry (kept, not deleted) |

Clients can be named by id (`s-and-s`), name (`S&S`) or alias.

## REST API

For scripts and automations (Zapier, n8n, a test tool webhook). Send `Authorization: Bearer <token>`. Interactive docs at `/api/docs`.

| | |
|---|---|
| `GET /api/clients` | Clients with stats |
| `POST /api/clients` | Create a client and its database (admin) |
| `PATCH /api/clients/{client}` | Change name, aliases, domains, or status `active` / `paused` (read-only) / `archived` (admin) |
| `GET /api/clients/{client}/context` | The context as markdown, plus the same as JSON |
| `GET /api/clients/{client}/entries` | List (`kind`, `since`, `until`, `tag`, `status`) or search (`q`) |
| `POST /api/clients/{client}/entries` | Create or update (201 created, 200 updated) |
| `GET`, `PATCH`, `DELETE /api/clients/{client}/entries/{id}` | Read, change, archive |
| `GET /api/clients/{client}/profile`, `PUT .../profile/{section}` | Profile |
| `GET`, `POST /api/tokens`, `DELETE /api/tokens/{id}` | Tokens (admin) |

Example: log a finished test from a script.

```bash
curl -X POST "$PACER_BASE_URL/api/clients/s-and-s/entries" \
  -H "Authorization: Bearer $PACER_BASE_TOKEN" -H "Content-Type: application/json" \
  -d '{"kind": "test", "title": "CART-02 free-shipping message", "status": "finished",
       "summary": "Ended 14 Oct: +3.1% CVR, 92% chance to beat control, 18k sessions per arm. Rolling out to 100%.",
       "source": "intelligems", "external_id": "exp_123", "url": "https://app.intelligems.io/...",
       "occurred_at": "2026-10-14T00:00:00Z"}'
```

## Admin commands

All talk to the API, so they work from any laptop with `PACER_BASE_URL` and `PACER_BASE_TOKEN` set:

```
pacer-base clients                          list clients
pacer-base add-client NAME [--slug] [--alias ...] [--domain ...]
pacer-base update-client CLIENT [--name] [--alias ...] [--domain ...] [--status active|paused|archived]
pacer-base add-token LABEL [--scope read|write|admin] [--client SLUG ...]
pacer-base tokens                           list tokens (never shows the secrets)
pacer-base revoke-token ID
pacer-base context CLIENT                   print what Claude sees for a client
```

`pacer-base serve` and `pacer-base migrate` run on the server (they need `DATABASE_URL`).

## Security and data

- **A database per client.** A request for S&S opens a connection to `pb_s_and_s` and nothing else, so one client's data can't leak into another's results. A client can be exported on its own (`pg_dump pb_s_and_s`) or removed with `DROP DATABASE`. The control database holds only the client list and token hashes.
- **Tokens** are random, shown once, and stored only as SHA-256 hashes. Scopes: `read`, `write` (read plus logging), `admin` (plus clients and tokens). A token limited to some clients can't tell that the others exist. Revoking takes effect within 30 seconds.
- **The URL form** (`/mcp/<token>`) is there for claude.ai. The token can show up in Railway's HTTP logs, which only people with access to the Railway project can see (and they can see the database anyway). Prefer the header form wherever headers are supported, and rotate the connector token if the URL is shared by mistake.
- **No client content in this repo.** It's public, so it holds only code. Client data lives in the Railway database.
- Pacer Base doesn't call any AI model itself. Summaries are written by Claude (or whoever logs the entry) when the thing is created.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
```

The tests need a throwaway Postgres server (they create and drop their own databases on it). With Docker:

```bash
./scripts/test-db.sh
```

then run the `export` line it prints, and:

```bash
pytest -q
```

The tests cover the REST API, client isolation (checked in the databases themselves), token scopes and client limits, de-duplication, search, profile history, restarts, and the connector end to end through a real server and the official MCP client, with the token in the header and in the URL. GitHub Actions runs them on every push.

```
pacer_base/
  app.py         REST API, health check, mounts the MCP endpoint
  mcp_server.py  connector tools and their instructions
  auth.py        tokens, scopes, the /mcp/<token> handling
  store.py       queries (registry, entries, profile, search)
  db.py          connection pools, one database per client, migrations
  schema.py      SQL migrations (append only)
  render.py      the markdown Claude reads
  models.py      kinds, profile sections, input validation
  cli.py         pacer-base command
skill/pacer-base/SKILL.md
```

Schema changes: append a migration to `CONTROL_MIGRATIONS` or `CLIENT_MIGRATIONS` in `schema.py`. On start the service applies new ones to the control database and every client database.

## Next steps after the pilot

- **Sign-in instead of a shared token.** OAuth with Google sign-in limited to Pacer accounts, so each person connects as themselves and entries record who logged them.
- **Scheduled Granola sync** (a scheduled Claude task running the skill's sync each evening), and webhooks from Intelligems or Airtable for test status.
- **Semantic search** (pgvector) once there's enough history for keyword search to miss things.
- Add the "log to Pacer Base" step to the results-slide, test-results and audit skills.
