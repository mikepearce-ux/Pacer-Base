---
name: pacer-base
description: Read and keep up to date Pacer's per-client context in Pacer Base (profile, plus summaries of and links to calls, tests, reports, audits and docs). Use before any work on a Pacer client (call prep, test ideas, results slides, audits, proposals, reports, "what do we know about X"), after creating a deliverable for a client, and when asked to sync a client's Granola meetings or "log this to Pacer Base".
---

# Pacer Base

Pacer Base is the team's shared memory for each client, reached through the
**Pacer Base** connector. Each client has:

- a **profile**: `overview`, `people` (who's who), `goals`, `current_tests`,
  `numbers` (numbers we know, with dates and sources), `issues`, `promises`,
  `links` (Drive folder, Airtable base, Monday board, store, testing tool)
- a timeline of **entries**: `call`, `test`, `report`, `audit`, `proposal`,
  `doc`, `decision`, `note`. Each is a title, a short summary someone could act
  on, and a link to the real thing.

Each client lives in its own database. Work on one client at a time and never
carry one client's details into another client's entries or answers.

## 1. Read before working on a client

1. `get_client_context` with the client's name or id (e.g. "S&S"). Read the profile and recent activity.
2. Look for anything specific with `search_client_context` (words, a test id, a page name) or `list_entries` (e.g. `kinds: ["test"]`, `status: "live"`).
3. Open the full entry with `get_entry` when the summary isn't enough; follow its link (Drive, Granola, Airtable) with the matching connector if you need the source.

When you use what you found, say where it came from and how old it is
("Pacer Base, call on 4 Nov: CVR 2.1%"). Treat figures older than about 60 days
as possibly stale and say so. If Pacer Base and a live source (GA4, Shopify,
Intelligems) disagree, trust the live source and offer to update the profile.

## 2. Write when something is created or decided

Log an entry with `log_entry` whenever, for a client, you:

- finish a deliverable (results slide, audit, proposal, report, test brief): `url` = the file's link
- read or summarise a call or meeting: see the Granola sync below
- see a test start, change or finish: `kind: "test"`, `status` = planned / live / finished, `external_id` = the testing tool's id (e.g. the Intelligems experience id) with `source: "intelligems"`
- hear of a decision worth remembering: `kind: "decision"`

Good entries:

- **title**: specific. "PDP-04 sticky add-to-cart: results", not "Test results".
- **summary**: 2-5 sentences that stand alone. What it is, the key numbers (with period and source), what was decided, what happens next and who owns it.
- **body** (optional): the detail worth searching later: decisions, actions with owners and dates, figures quoted. Keep it to what matters, not a full transcript.
- **occurred_at**: when it happened, not when you logged it.
- **url**: always include the link when there is one.

Logging the same `url` (same kind) or the same `source` + `external_id` again
updates that entry, so re-running a sync or a skill never creates duplicates. To
change one field of an existing entry, pass `entry_id` and only that field.

Keep the profile current with `update_profile_section`. It replaces the whole
section, so read it first (`get_client_context`) and send the complete new text.
Update it when something durable changes: a new contact, a new goal, a test
starting or finishing (`current_tests`), a new figure with date and source
(`numbers`), a promise made or kept (`promises`). Mention the change in your reply.

After writing, tell the user what you logged, with the entry number.

## 3. Sync a client's Granola meetings

When asked to "sync S&S meetings from Granola" (or after reading a client meeting in Granola):

1. `get_client_context` for the client: note its **domains** and **aliases**, and the date of the latest `call` entry from Granola (`list_entries` with `kinds: ["call"]`).
2. Find the meetings in Granola with `search_meetings`: once with `participant` set to each client domain, and once with `keywords` set to the client's name and aliases, `from_date` = the last synced call date (or the date the user gives). Merge the results; skip internal meetings that only mention the client in passing unless they contain decisions about the client.
3. Read them with `get_meetings` (up to 10 IDs per call). With Granola's paid plan, use `get_meeting_transcript` for exact figures or wording the notes leave out.
4. For each meeting, `log_entry`:
   - `kind: "call"`, `source: "granola"`, `external_id`: the Granola meeting id
   - `url`: `https://notes.granola.ai/d/<meeting id>`
   - `title`: the meeting title; `occurred_at`: the meeting's start time
   - `summary`: what was discussed and decided, figures quoted, next steps
   - `body`: decisions, actions (owner, due date), open questions, claims or figures worth checking
5. Update the profile sections the meetings changed (`current_tests`, `numbers`, `promises`, `people`), then report what was synced: number of meetings, entries created vs updated, profile changes.

## Don'ts

- Don't log passwords, API keys, payment details or personal data beyond names, roles and work emails.
- Don't invent numbers. If a figure has no date or source, say so in the entry.
- Don't paste whole documents or transcripts into `body`; link to them.
- Don't archive entries unless the user asks or an entry is clearly wrong; `archive_entry` hides it but keeps it.
