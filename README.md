# Pacer Base

Pacer's shared memory for each client. Every client gets **its own Postgres database** holding:

- a **profile**: who's who, goals, current tests, numbers we know, issues, promises, key links
- a timeline of **entries**: a short summary of each call, Slack day, test, report, audit, proposal or doc, with a link to the original and links to related things (the Monday item, the Slack thread, the test)
- a **Where to look** list: the client's Slack channels, Drive folder, Monday board, test tool and meeting domain
- **changes waiting for approval**: suggested profile edits that an approver hasn't confirmed yet

Claude reads and writes it through the **Pacer Base connector** (an MCP server). The **pacer-base skill** tells Claude when to read it, how to dig further, and what to log. Two **scheduled runs** keep it current: an evening round-up and a morning approval check. Scripts can use the REST API.

```
 Slack (client + agency channels) ─┐
 Granola meetings ─────────────────┤   evening      ┌──────────────┐      ┌─ pb_<client a>
 Drive (approved docs) ────────────┼─ round-up ───▶│  Pacer Base  │──────┼─ pb_<client b>
 Monday board ─────────────────────┤  (Claude)      │  (Railway)   │      └─ control db: clients,
 Intelligems tests ────────────────┘                └──────┬───────┘         tokens, settings
                                                           │ changes to approve
   #pacer-base-review  ◀── posts ✅/❌ requests ────────────┘
          │ approvers react
          └──────────────▶ morning check (Claude) applies what was approved
```

Pilot: **S&S**.

## How information gets in

1. **As people work.** When Claude makes a deliverable for a client (results slide, audit, proposal), it logs the link and a summary.
2. **The evening round-up** (weekdays, about 6pm). For each client, Claude reads **only** the sources listed for that client, from where it stopped last time:
   - **Slack:** the client's channels (internal, and shared with the client), plus the agency-wide channels, from which it takes only messages about that client.
   - **Granola:** meetings with anyone from the client's email domain, or about them.
   - **Drive:** new or changed files in the client's folder (everything there counts as approved).
   - **Monday:** new, finished and overdue items on the client's board (summarised and linked; tasks stay in Monday).
   - **Intelligems:** tests starting, changing or ending.

   It logs one card per meeting, file or test, and one per channel per day. Then it suggests profile changes and posts them to the review channel.
3. **The morning approval check** (weekdays, about 8am). Claude reads the reactions and replies on each request and acts on the approvers' responses:
   - ✅ applies the change.
   - ❌ rejects it, and it won't be suggested again from the same source.
   - A reply with a correction applies the corrected version.
   - Anyone who isn't an approver is ignored.
   - After 7 days without a decision, the request lapses.

Re-running anything is safe: an entry with the same source + id, or the same link and kind, is updated rather than duplicated.

## How Claude uses it

`get_client_context` returns the profile, what's waiting for approval, Where to look, and recent activity. Every item carries its links. Pacer Base is the map; the systems hold the detail. So when something looks relevant, Claude follows the links with the matching connector (the Monday item, the Slack thread, the live test) before answering, and says where each fact came from and how old it is.

## What's stored

**Profile sections:** `overview`, `people`, `goals`, `current_tests`, `numbers`, `issues`, `promises`, `links`. Old text is kept in history when a section changes.

**Entry kinds:**

| Kind | For |
|---|---|
| `call` | A call or meeting with the client (or internal, about them) |
| `test` | An A/B or CRO test: hypothesis, setup, status, result |
| `report` | A performance report, results slide or read-out |
| `audit` | A CRO audit, diagnostic or research piece |
| `proposal` | A proposal, roadmap, SOW or pitch |
| `doc` | Any other document worth finding again |
| `decision` | A decision and why it was made |
| `note` | Anything else (e.g. a day's Slack summary) |

**Sources** (per client, admin-only): `slack`, `granola`, `drive`, `monday`, `intelligems`, `airtable`, `shopify`, `ga4`, `other`, each with its id, name, link and role (e.g. "shared with client").

**Settings** (agency-wide, admin-only): `approvers` (name and Slack user id), `review_channel`, `agency_channels`, `daily_entry_cap` (default 50), `proposal_expiry_days` (default 7).

## Guardrails

- **A database per client.** A request for one client opens that client's database and nothing else, so data can't leak between clients. A client can be exported (`pg_dump pb_<client>`) or removed on its own.
- **Only listed sources.** The round-up reads only a client's listed sources plus the agency channels. Only admins can change those lists, not Claude.
- **Approval for facts.** Cards are saved automatically (they're summaries with links back). Profile changes wait for an approver. Only approve-level tokens can apply them, and only for a named approver.
- **Nothing deleted.** Entries can only be hidden, and old profile text is kept.
- **Limits.** By default each writer can add 50 new entries per client per day (updates don't count), and the round-up stops at 30 per client per run.
- **Untrusted text stays untrusted.** Messages, notes and documents are treated as information, never instructions. The runs post only in the review channel: they never message clients, edit documents, or read DMs or channels that aren't listed.
- **Tokens** are random, shown once, and stored only as SHA-256 hashes. Scopes, each including the last: `read`; `write` (log entries, propose changes); `approve` (also approve, and edit the profile directly); `admin` (also clients, sources, settings, tokens). A token can be limited to some clients, and then can't tell that the others exist.
- **No client data in this repo.** It's public, so it holds only code. Client names, channel ids and people live in the database and in your private setup file.

## Deploy on Railway

1. **New project** > **Deploy from GitHub repo** > `mikepearce-ux/Pacer-Base`. Railway reads `railway.json` and builds the `Dockerfile`.
2. In the project, **+ New** > **Database** > **PostgreSQL**.
3. On the Pacer Base service, **Variables**:
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`
   - `PACER_BASE_PUBLIC_URL` = `https://<your-domain>` (optional; used in the sign-in handshake's metadata)
   - `PACER_BASE_ADMIN_TOKEN` = a long random string, kept in the team password manager:
     ```bash
     python3 -c "import secrets; print(secrets.token_urlsafe(32))"
     ```
4. **Settings** > **Networking** > **Generate Domain**.
5. Open `https://<your-domain>/health`; it should say `{"ok": true, ...}`.
6. On the Postgres service, turn on backups for its volume.

Tables are created on start, and each client's database when the client is added.

## Set up a client

On your laptop (Python 3.11+):

```bash
pip install "git+https://github.com/mikepearce-ux/Pacer-Base@claude/lucid-planck-v30od9"
```

```bash
export PACER_BASE_URL=https://<your-domain> PACER_BASE_TOKEN=<admin token>
```

Write a **setup file** (keep it out of this repo). It holds the agency settings and, per client, its names, email domains and sources:

```json
{
  "settings": {
    "approvers": [{"name": "<name>", "slack_user_id": "U..."}],
    "review_channel": {"id": "C...", "name": "#pacer-base-review"},
    "agency_channels": [{"id": "C...", "name": "#pacer-all"}]
  },
  "clients": [{
    "name": "<Client name>", "slug": "<client-id>", "aliases": ["<short name>"], "domains": ["<client-domain.com>"],
    "sources": [
      {"system": "slack", "ref": "C...", "name": "#<client channel>", "role": "shared with client"},
      {"system": "slack", "ref": "C...", "name": "#internal-<client>", "role": "internal"},
      {"system": "granola", "ref": "<client-domain.com>", "role": "attendee email domain"},
      {"system": "drive", "ref": "<folder id>", "name": "Clients/<client>", "url": "https://drive.google.com/drive/folders/<folder id>", "role": "approved documents"},
      {"system": "monday", "ref": "<board id>", "name": "<board name>"},
      {"system": "intelligems", "ref": "<organisation id>"}
    ]
  }]
}
```

```bash
pacer-base setup pacer-setup.json
```

It's safe to re-run after editing the file. Then make a token per person. Approvers get `approve`; everyone else gets `write`:

```bash
pacer-base add-token "<name>" --scope approve
```

Each command prints the token once, plus the claude.ai connector URL and the Claude Code command for it. The scheduled runs use the connection of the person who sets them up, so that person needs an `approve` token.

## Connect Claude

**claude.ai:** add a custom connector (Settings > Connectors) with the URL printed for your token:

```
https://<your-domain>/mcp/<token>
```

claude.ai connectors can't send custom headers, so the token goes in the URL. Treat it like a password.

There's no sign-in. claude.ai still runs a sign-in handshake when you press **Connect**, even for servers without one, and fails with "Couldn't register with …'s sign-in service" if nothing answers it. So Pacer Base answers it automatically (`pacer_base/oauth.py`): registration, an authorize step that approves straight away, and a token exchange with PKCE. A window may flash open and closed; nothing to type. The token from that handshake grants nothing by itself. Access is still the key in the URL, and the handshake is only offered for a URL whose key is valid.

**Claude Code:**

```bash
claude mcp add --transport http pacer-base https://<your-domain>/mcp --header "Authorization: Bearer <token>"
```

**Skill:** add `skill/pacer-base/` as a skill (in claude.ai, upload the folder as a zip; in Claude Code, copy it to `~/.claude/skills/pacer-base/`).

## Schedule the runs

Create two scheduled Claude tasks, weekdays in UK time, each starting a fresh session with these connectors: Pacer Base, Slack, Granola, Google Drive, monday.com and Intelligems (the morning one needs only Pacer Base and Slack).

| Task | When | Instructions |
|---|---|---|
| Pacer Base evening round-up | about 6pm | [`routines/evening-roundup.md`](routines/evening-roundup.md) |
| Pacer Base morning approvals | about 8am | [`routines/morning-approvals.md`](routines/morning-approvals.md) |

To stop them, pause the two tasks. The first round-up reads back 7 days. For an earlier catch-up, ask Claude to run the round-up for a client from a given date.

## Connector tools

| Tool | Access | Does |
|---|---|---|
| `list_clients` | read | Clients this token can use, with entry counts |
| `get_client_context` | read | Profile, waiting changes, Where to look, recent activity. Claude calls this first |
| `search_client_context` | read | Full-text search of one client's entries |
| `list_entries` | read | Timeline filtered by kind, date, tag or status |
| `get_entry` | read | One entry in full, with its links |
| `list_proposals` | read | Profile changes (waiting, or decided) with current and proposed text |
| `get_sync_plan` | read | For the scheduled runs: sources, where to read from, agency channels, approvers, limits |
| `log_entry` | write | Add or update an entry, with related links |
| `archive_entry` | write | Hide an entry (kept, not deleted) |
| `propose_profile_change` | write | Suggest a new section text for approval |
| `attach_review_message` | write | Record where an approval request was posted |
| `mark_source_checked` | write | Record how far a source has been read (listed sources only) |
| `decide_proposal` | approve | Apply or reject a waiting change, for a named approver |
| `update_profile_section` | approve | Replace a section directly |

Clients can be named by id, name or alias.

## REST API

For scripts and automations. Send `Authorization: Bearer <token>`; interactive docs at `/api/docs`.

| | |
|---|---|
| `GET /api/clients`, `POST` (admin), `PATCH /api/clients/{client}` (admin) | Clients |
| `GET /api/clients/{client}/context` | The context as markdown, plus JSON |
| `GET /api/clients/{client}/entries` (`kind`, `since`, `until`, `tag`, `status`, or `q` to search), `POST` | Entries (201 created, 200 updated) |
| `GET`, `PATCH`, `DELETE /api/clients/{client}/entries/{id}` | Read, change, archive |
| `GET /api/clients/{client}/profile`, `PUT .../profile/{section}` (approve) | Profile |
| `GET /api/clients/{client}/proposals?status=`, `POST` | Profile changes |
| `POST .../proposals/{id}/decision` (approve), `POST .../proposals/{id}/review` | Decide; record the review message |
| `GET`, `PUT /api/clients/{client}/sources` (admin) | Sources |
| `GET /api/settings`, `PUT /api/settings/{key}` (admin) | Settings |
| `GET`, `POST /api/tokens`, `DELETE /api/tokens/{id}` (admin) | Tokens |

## Admin commands

They all talk to the API, so they work from any laptop with `PACER_BASE_URL` and `PACER_BASE_TOKEN` set:

```
pacer-base setup FILE                       settings, clients and sources from a setup file
pacer-base clients | sources CLIENT | settings | proposals CLIENT [--status all]
pacer-base add-client NAME [--slug] [--alias ...] [--domain ...]
pacer-base update-client CLIENT [--name] [--alias ...] [--domain ...] [--status active|paused|archived]
pacer-base set-sources CLIENT FILE          replace a client's sources
pacer-base set-setting KEY JSON             e.g. daily_entry_cap 40
pacer-base add-token LABEL [--scope read|write|approve|admin] [--client SLUG ...]
pacer-base tokens | revoke-token ID
pacer-base context CLIENT                   what Claude sees for a client
```

`pacer-base serve` and `pacer-base migrate` run on the server.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
```

The tests need a throwaway Postgres server (they create and drop their own databases). With Docker:

```bash
./scripts/test-db.sh
```

then run the `export` line it prints, and:

```bash
pytest -q
```

The tests cover the REST API, client isolation (checked in the databases themselves), scopes and client limits, de-duplication, search, profile history, proposals and approval (approvers only, corrections, rejection memory, superseding, conflicts, expiry), sources, settings, the daily cap, the setup command, and the connector end to end through a real server and the official MCP client, including a full round-up and morning-check pass. GitHub Actions runs them on every push.

```
pacer_base/
  app.py         REST API, health check, mounts the MCP endpoint
  mcp_server.py  connector tools and their instructions
  auth.py        tokens, scopes, the /mcp/<token> handling
  store.py       queries: registry, settings, entries, profile, sources, proposals, search
  db.py          connection pools, one database per client, migrations
  schema.py      SQL migrations (append only)
  render.py      the markdown Claude reads
  models.py      kinds, sections, sources, settings, validation
  cli.py         pacer-base command
routines/        instructions for the two scheduled runs
skill/pacer-base/SKILL.md
```

Schema changes: append a migration to `CONTROL_MIGRATIONS` or `CLIENT_MIGRATIONS` in `schema.py`; on start the service applies new ones to the control database and every client database.

## Next steps after the pilot

- **Sign-in instead of tokens in URLs:** Google sign-in limited to Pacer accounts, so each person connects as themselves.
- **Approve from a page** as well as from Slack.
- **Semantic search** (pgvector) once there's enough history for keyword search to miss things.
- Add a "log to Pacer Base" step to the results-slide, test-results and audit skills.
