# Pacer Base

Pacer's shared memory for each client. Every client gets **its own Postgres database** holding:

- a **profile**: overview, who's who, goals, current tests, numbers we know, known issues, what we've promised, **systems** (the tech set-up) and key links
- a timeline of **entries**: a short summary of each call, Slack day, test, report, audit, proposal or doc, with a link to the original and links to related things (the Monday item, the Slack thread, the test)
- a **Where to look** list: the client's Slack channels, Drive folder, Monday board, test tool and meeting domain
- **changes waiting for approval**: suggested profile edits that an approver hasn't confirmed yet

Claude reads and writes it through the **Pacer Base connector** (an MCP server), and runs heat maps through the **Attention Lab connector** on the same server. Three skills tell Claude how to use them: **pacer-base** (when to read it, how to dig further, what to log), **attention-lab** (heat maps) and **pacer-base-routines** (the scheduled runs and each person's weekly). Two **agency scheduled runs** keep it current: an evening round-up and a morning approval check. Scripts can use the REST API.

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

Clients: Stripe & Stare, The Body Shop, Universal Works, Living Planet (incl. Planet Paleo), Innermost, Smart Salem and Cutler and Gross (prospect), plus **Pacer (internal)** (`pacer`): the agency's own space for its tools, systems, how-tos and agency-wide decisions.

## How information gets in

1. **As people work.** When Claude makes a deliverable for a client (results slide, audit, proposal, release notes, design brief), it logs the link and a summary under the person's name (`logged_by`). Each person also runs a **personal weekly** (Fridays) that logs their own Drive files and Granola notes the round-up can't see. The results portal and Attention Lab log their own write-ups and heat maps automatically.
2. **The evening round-up** (weekdays, 5:36pm UK). For each client, Claude reads **only** the sources listed for that client, from where it stopped last time:
   - **Slack:** the client's channels (internal, and shared with the client), plus the agency-wide channels, from which it takes only messages about that client.
   - **Granola:** meetings with anyone from the client's email domain, or about them.
   - **Drive:** new or changed files in the client's folder (everything there counts as approved).
   - **Monday:** new, finished and overdue items on the client's board (summarised and linked; tasks stay in Monday).
   - **Intelligems:** tests starting, changing or ending.

   It logs one card per meeting, file or test, and one per channel per day. Then it suggests profile changes and posts them to the review channel.
3. **The morning approval check** (weekdays, 7:34am UK). Claude reads the reactions and replies on each request and acts on the approvers' responses:
   - ✅ applies the change.
   - ❌ rejects it, and it won't be suggested again from the same source.
   - A reply with a correction applies the corrected version.
   - Anyone who isn't an approver is ignored.
   - After 7 days without a decision, the request lapses.

Re-running anything is safe: an entry with the same source + id, or the same link and kind, is updated rather than duplicated. A thing logged from two places stays one entry: when the portal or Attention Lab logs a link that was already logged from Slack, it takes over that entry; when someone logs a link that a system already owns, the system's summary is kept and their related links and tags are added.

## How Claude uses it

`get_client_context` returns the profile, what's waiting for approval, Where to look, and recent activity. Every item carries its links. Pacer Base is the map; the systems hold the detail. So when something looks relevant, Claude follows the links with the matching connector (the Monday item, the Slack thread, the live test) before answering, and says where each fact came from and how old it is.

## What's stored

**Profile sections:** `overview`, `people`, `goals`, `current_tests`, `numbers`, `issues`, `promises`, `systems`, `competitors`, `links`. `systems` is the client's tech set-up: platform and stores, theme and where its code lives, apps and integrations, environments, who owns what at Pacer and the client, and how to get access (never passwords or keys). Old text is kept in history when a section changes.

**Who logged it:** each entry and proposal records its author: the key's label, or `<name> (via <key>)` when Claude passes the person's name with `logged_by`, so cards made through the shared team key still say who they came from. The daily cap counts per author.

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
| `competitor` | Something a competitor did (promotion, price change, launch, site or email change, ad or social campaign), with a "so what" for the client |

**Sources** (per client, admin-only): `slack`, `granola`, `drive`, `monday`, `intelligems`, `airtable`, `shopify`, `ga4`, `competitor`, `other`, each with its id, name, link and role (e.g. "shared with client").

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

Each command prints the token once, plus the claude.ai connector URL and the Claude Code command for it. The org connectors use a shared `write` key; the morning approvals use an approver's own `approve` key through a personal connector (see Keys in use below).

## Connect Claude

**claude.ai:** an owner adds **Pacer Base** and **Attention Lab** as organisation connectors (Organization settings › Connectors) with the team key's URLs, and everyone clicks Connect. Approvers add their own approve-key URL as a personal connector (Customize › Connectors) named "Pacer Base approver". The URL looks like:

```
https://<your-domain>/mcp/<token>
```

claude.ai connectors can't send custom headers, so the token goes in the URL. Treat it like a password.

There's no sign-in. claude.ai still runs a sign-in handshake when you press **Connect**, even for servers without one, and fails with "Couldn't register with …'s sign-in service" if nothing answers it. So Pacer Base answers it automatically (`pacer_base/oauth.py`): registration, an authorize step that approves straight away, and a token exchange with PKCE. A window may flash open and closed; nothing to type. The token from that handshake grants nothing by itself. Access is still the key in the URL, and the handshake is only offered for a URL whose key is valid.

**Keys in use at Pacer:**

| Key | Scope | Used by |
|---|---|---|
| Pacer team | write | The org connectors **Pacer Base** and **Attention Lab** (everyone) |
| <approver> (approver) | approve | That approver's own **personal** connector, named "Pacer Base approver", and the morning approvals run. Never an org connector, or everyone could approve |
| Daily round-up | write | Round-up and catch-up runs outside claude.ai |
| Attention Lab | write | Attention Lab logging heat maps |
| Results portal | write | The portal syncing write-ups |

**Claude Code:**

```bash
claude mcp add --transport http pacer-base https://<your-domain>/mcp --header "Authorization: Bearer <token>"
```

**Skills:** `skill/pacer-base/`, `skill/attention-lab/` and `skill/pacer-base-routines/`. In claude.ai, zip each folder and upload it in **Organization settings › Plugins & skills** so everyone has it; in Claude Code, copy them to `~/.claude/skills/`.

## Schedule the runs

Two agency scheduled tasks, set up once (by Mike), each starting a fresh session. Their prompts say "use the pacer-base-routines skill and run the …", so the instructions live in the skill (and in [`routines/`](routines/)).

| Task | When (UK) | Connectors | Suggested model |
|---|---|---|---|
| Pacer Base evening round-up | weekdays 5:36pm | Pacer Base, Slack, Granola, Google Drive, monday.com, Intelligems | Opus 5.5 |
| Pacer Base morning approvals | weekdays 7:34am | Pacer Base approver, Slack | Sonnet 5.5 |

Don't set up a second round-up: two would read and post everything twice.

To stop them, pause the two tasks. The first round-up reads back 7 days. For an earlier catch-up, ask Claude to run the round-up for a client from a given date.

## Attention Lab connector (heat maps)

The same server also runs a second connector for Attention Lab, the heat mapper. Add it in claude.ai as its own custom connector (name it "Attention Lab"), using the same key:

```
https://<your-domain>/heatmap/mcp/<token>
```

| Tool | Does |
|---|---|
| `run_heatmap` | Heat map one page (desktop and mobile), wait for it, and return the written review, recommendations and the heat map images |
| `run_heatmaps` | Start up to 25 pages at once |
| `get_heatmap` | Read one result, with images |
| `list_heatmaps` | The 10 most recent |

It calls Attention Lab with one service key. Set these on the Pacer Base service:

- `ATTENTION_LAB_URL`: Attention Lab's public address
- `ATTENTION_LAB_TOKEN`: an Attention Lab API key (`ahm_…`) for a shared Pacer account

Attention Lab logs each finished heat map of a client site to that client's Pacer Base itself. It matches the client by the site's domain, or uses the client passed in. To switch that on, set `PACER_BASE_URL`, `PACER_BASE_TOKEN` (a write key) and `PUBLIC_BASE_URL` on Attention Lab. Each heat map gets a read-only share link (`/s/<token>`), so cards in Pacer Base and Slack open without a login. The skill is in `skill/attention-lab/`.

## Each person's own work

Claude works with each person's own access, so the pacer-base skill logs artifacts and docs as they're made for a client (once their links are shared with the team), under that person's name. It also tells each role what to log where: developers (releases, specs, set-up changes, plus the Systems section), owners (proposals, pricing, promises), account leads (wraps, calls), designers (briefs, Figma), CRO (audits, tests, results).

Everyone sets up a **personal weekly** in their own Claude: a scheduled task on Fridays at 4:47pm, on Sonnet 5.5, with Pacer Base, Google Drive and Granola, and the prompt *"I'm <name>, <role> at Pacer. Use the pacer-base-routines skill and run the personal weekly. This is an unattended scheduled run: don't ask questions, use judgement and finish."* It logs their client docs and calls that Pacer Base doesn't have yet, and lists anything only in their My Drive so they can move it to the client's folder.

Two habits make most of this automatic: save client docs in the client's folder under Pacer › Clients (the round-up reads it), and share client call notes to the Pacer team space in Granola.

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
| `log_entry` | write | Add or update an entry, with related links; `logged_by` names the person |
| `archive_entry` | write | Hide an entry (kept, not deleted) |
| `propose_profile_change` | write | Suggest a new section text for approval; `logged_by` names the person |
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
routines/        instructions for the scheduled runs and the personal weekly
skill/pacer-base/            the main skill
skill/attention-lab/         heat maps
skill/pacer-base-routines/   the routines as a skill (references/ holds copies of routines/)
```

Schema changes: append a migration to `CONTROL_MIGRATIONS` or `CLIENT_MIGRATIONS` in `schema.py`; on start the service applies new ones to the control database and every client database.

## Next steps

- **Sign-in instead of tokens in URLs:** Google sign-in limited to Pacer accounts, so each person connects as themselves.
- **Approve from a page** as well as from Slack.
- **Semantic search** (pgvector) once there's enough history for keyword search to miss things.
- Add a "log to Pacer Base" step to the results-slide, test-results and audit skills.
- **Granola Pro**, so the runs can read more than the last 30 days of meetings and use transcripts.
- **A UK proxy for Attention Lab** (`CAPTURE_PROXY_URL`), so heat maps see UK prices and no location prompts.
