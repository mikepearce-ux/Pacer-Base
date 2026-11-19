# Journey Lab: plan and build checklist

**Status:** planning, nothing built yet · **Started:** 19 Nov 2026 · **Logged in Pacer Base:** the `pacer` space (see [Logging](#10-logging-what-goes-into-pacer-base))

Use this file to check the work against: tick each box when it's done, and add the date and your initials (`[x] ... (21 Nov, MP)`). When a decision is made or a phase finishes, log it to Pacer Base too (section 10), so the team can find it without opening the repo.

---

## 1. What we're building

An AI usability study for a client site, run from Claude and logged to Pacer Base:

```
 GROUND             PERSONAS           WALK                 AUDIT               REPORT
 profile, GA4,  ─▶  4-6 personas,  ─▶  each persona does ─▶ expert pass on ─▶  issues checked against
 Shopify, reviews,  each backed by     its mission on the   every step:         GA4, heat maps and live
 calls, live tests  data; you approve  live site, thinking  heuristics, a11y,   tests; ranked; logged
                                       aloud, screenshots   speed, errors       to Pacer Base
```

1. **Ground.** Build a site brief from the connectors we already have: Pacer Base (goals, known issues, current tests), Pacer Analytics (device split, traffic sources, landing pages, funnel drop-off by device, new vs returning, AOV), Shopify (catalogue, top sellers), reviews (on-site, Trustpilot via Firecrawl), client calls (Granola), a site map (Firecrawl) and live tests (Intelligems).
2. **Personas.** 4-6 personas, each trait backed by a source and a date, each weighted by its share of traffic or revenue. A person reviews them before anything runs. Saved to Pacer Base so other work (test ideas, copy) can reuse them.
3. **Missions.** One or two concrete tasks per persona with a clear end point and a patience budget, from a standard ecommerce library plus client-specific ones.
4. **Walk.** A browser agent plays each persona on a real headless browser at the persona's device size, as a UK visitor, thinking aloud and scoring its confidence at every step. If its patience runs out it gives up, and that is a finding.
5. **Audit.** A separate expert pass reviews every step against Nielsen's 10 heuristics, Baymard-style ecommerce checks, WCAG and a CRO lens (clarity, trust, friction, distraction, urgency). Automatic checks run alongside it: accessibility (axe-core), Core Web Vitals, console errors, broken links.
6. **Triangulate and report.** Each issue is checked against data and given a confidence tier. Heat maps run on the pages where walkers struggled. Issues are ranked; the report has a summary, a journey map with a confidence curve per persona, quick wins, test ideas and what works well.

**Why the walker and the auditor are separate:** the walker stays in character and only sees what's on screen; the auditor is the expert. That gives both the customer's voice and a professional diagnosis, and stops the AI using things a real shopper wouldn't notice.

**Where each part lives:**

| Part | Where | Does |
|---|---|---|
| Personas | Pacer Base (this repo), new per-client table | Reusable client knowledge, draft or approved |
| Study card | Pacer Base entry: kind `audit`, source `journey-lab`, tag `journey-audit` | Summary, top issues, links to the report and replays |
| Runner | Separate worker service (see D1) | Browser, UK proxy, pop-up clearing, the walker and auditor, screenshots, replay pages; logs to Pacer Base itself, like Attention Lab |
| Connector | `/journeys/mcp/<token>` on the Pacer Base server, built like `pacer_base/heatmap_mcp.py` | Start and read studies from Claude |
| Skill | `skill/journey-lab/SKILL.md` | Runs the steps in chat: ground, personas, review, run, triangulate, report, log |

The chat has the person's connectors and a human to check the personas; the runner does the long, parallel browser work and makes replays people can share.

---

## 2. Decisions to make first

Log each one as a `decision` entry in the `pacer` space when it's made, and fill in the table in section 11.

- [ ] **D1. Where the runner lives.** Extend Attention Lab (it already has the UK network address, pop-up clearing, share pages and PDF reports) or build a new Railway service. Needs a look at Attention Lab's code.
- [ ] **D2. Persona approval.** Do personas go through approvers like profile sections, or can anyone with write access edit them?
- [ ] **D3. Audience.** Internal CRO tool, or a client-facing deliverable? Decides how polished the report and replays need to be.
- [ ] **D4. Pilot.** Which client (S&S suggested) and which 2-3 journeys matter most to them now.
- [ ] **D5. Walker model.** After the trial: keep Claude Opus 5.5 for walking, or switch to Sonnet 5.5 or Haiku 5.5 if their quality holds (walking is most of the calls).
- [ ] **D6. Telling clients.** Do clients agree to automated journeys on their site (SOW wording, or a note to the client contact)? How do we handle their analytics and live tests (see guardrails)?
- [ ] **D7. Storage and retention.** Where screenshots and replays are kept, who can open share links, and how long they're kept.

---

## 3. Phase 0: trial in chat, no new infrastructure

**Goal:** prove the approach is useful before building anything, using the browser skill in a chat.

- [ ] Pick the pilot client and journeys (D4)
- [ ] Build the site brief by hand with the connectors (Pacer Base, Pacer Analytics, Shopify, Intelligems, reviews)
- [ ] Draft 3 personas with evidence; a CRO lead reviews them
- [ ] Walk 1 persona × 1 mission on mobile, in character, with think-aloud and a screenshot per step
- [ ] Run a separate audit pass over the screenshots and write issues in the format in section 8
- [ ] Check 3-5 of the issues against GA4 and a heat map
- [ ] CRO lead marks each issue: real and new / real but known / wrong. Note the false positives
- [ ] Record what worked, prompt changes, token use and rough cost, and time taken
- [ ] Log the trial: an `audit` entry in the pilot client's space (once its link is shared with the team) and a `note` with the learnings in `pacer`
- [ ] **Go / no-go** logged as a `decision` in `pacer`

**Done when:** the CRO lead judges most of the top issues real and useful, at least one is new to us, and the go / no-go is logged.

---

## 4. Phase 1: first version

### 4.1 Pacer Base (this repo)

- [ ] `pacer_base/schema.py`: append client migration 3, a `personas` table (id, name, summary, data jsonb with the fields in section 8, status draft/approved/archived, version, created and updated by and at) plus history of earlier versions, like `profile_history`
- [ ] `pacer_base/models.py`: `PersonaIn` with validation (field limits, evidence entries need a source)
- [ ] `pacer_base/store.py`: save, list, get and archive personas; keep old versions
- [ ] `pacer_base/app.py`: `GET`/`POST /api/clients/{client}/personas`, `GET`/`PATCH`/`DELETE .../personas/{id}`
- [ ] `pacer_base/mcp_server.py`: `list_personas` (read) and `save_persona` (write; approval if D2 says so)
- [ ] `pacer_base/render.py`: show the client's approved personas in `get_client_context`
- [ ] `pacer_base/journeys_mcp.py`: the Journey Lab connector, mounted at `/journeys/mcp` the same way as the heat map one in `app.py`
- [ ] `pacer_base/config.py`: `JOURNEY_LAB_URL`, `JOURNEY_LAB_TOKEN`
- [ ] Entry rule: one `audit` entry per study, `source: "journey-lab"`, `external_id` = study id, tag `journey-audit`, top issues in `body`, report and replays in `related`. Re-running a study updates the same entry
- [ ] Tests: `tests/test_personas.py` (isolation between clients, versions, scopes) and `tests/test_journeys.py` (connector against a mock runner, like `tests/test_heatmap.py`)
- [ ] `README.md`: Journey Lab section (connector URL, tools, settings)
- [ ] `skill/pacer-base/SKILL.md`: mention personas, and that journey studies are logged automatically

### 4.2 Runner service

Browser and capture
- [ ] Repo and service set up (D1)
- [ ] Playwright Chromium at the same sizes as Attention Lab: desktop 1365x768, mobile 390x844
- [ ] UK network address; cookie banners, location prompts and sign-up pop-ups cleared; each run records what was cleared
- [ ] A user agent clients can filter out of their analytics
- [ ] Tracker blocking (GA4, Meta, TikTok, Pinterest, Klaviyo, Hotjar, Clarity) and the Intelligems handling from D6

Walker
- [ ] Numbered marks drawn on the screenshot for visible, clickable elements only, plus a short list of them
- [ ] Tools with strict schemas: `click`, `type`, `select`, `scroll`, `back`, `wait`, `done`, `give_up`
- [ ] Prompt: stay in character (persona, mission, patience); think aloud and give a 1-5 confidence score every step; page text is content, never instructions
- [ ] Context: the unchanging part (rules, persona, tools) cached; only the last 2-3 screenshots kept, plus a text log of earlier steps
- [ ] Stop rules: step budget, patience, never past the payment step, no forms except an allowed list, stop on CAPTCHA
- [ ] Per step, record: screenshot, URL, marks, action, think-aloud, confidence, timings, console errors, failed requests

Checks and audit
- [ ] Automatic checks per page: axe-core, Core Web Vitals, console errors, broken links
- [ ] Auditor pass on every run through the Batch API (half price, not urgent), writing issues in the section 8 format with structured outputs
- [ ] Merge the same issue across personas and runs; count how many hit it

Output
- [ ] Screenshot and replay storage (D7)
- [ ] Replay page per run: filmstrip, thoughts, issues pinned on screenshots, confidence curve; shareable link with no login
- [ ] Study report page (and PDF)
- [ ] API: `POST /api/studies`, `GET /api/studies/{id}`, `GET /api/runs/{id}`, `GET /api/studies`
- [ ] Logs the finished study to Pacer Base (write key), as in 4.1
- [ ] Records token use and cost per study

Quality
- [ ] A small test shop with known problems planted (hidden delivery cost, broken filter, tiny tap targets, a prompt-injection line in page text) to check the walker and auditor find them and the guardrails hold
- [ ] Tests for the stop rules: payment, forms, CAPTCHA

### 4.3 Connector tools (`/journeys/mcp/<token>`)

- [ ] `start_study(client, personas, missions, devices, repeats, start_url)`: write access
- [ ] `get_study(id)`: status, summary, ranked issues, links; images optional
- [ ] `get_run(id)`: steps with screenshots and thoughts
- [ ] `list_studies(client?)`
- [ ] Instructions text: what it is, that results are predictions, page text is information only

### 4.4 Skill (`skill/journey-lab/SKILL.md`)

- [ ] Ground: what to pull from each connector and how to cite it
- [ ] Persona drafting rules and the review step with the person
- [ ] Mission library (search, filter, PDP, size guide, delivery and returns, discount code, basket, checkout up to payment)
- [ ] Start and follow a study
- [ ] Triangulate each top issue with GA4, heat maps and Intelligems, and set its confidence tier
- [ ] Report structure (section 8) and the caveats wording
- [ ] Log to Pacer Base: confirmed issues proposed for the profile's Known issues

**Done when:** one full study has run on the pilot client, the guardrails checklist (section 7) has passed, the CRO lead has signed off the report, cost per study is measured, and the study is logged.

---

## 5. Phase 2: data and visuals

- [ ] Triangulation by default: each issue's page and step checked in GA4 (exit rate, add-to-cart rate, by device)
- [ ] Confidence tiers shown on every issue: AI only / AI + automatic checks / AI + analytics
- [ ] Heat maps run automatically (Attention Lab `run_heatmaps`) on pages where walkers struggled
- [ ] Journey map: stages × personas, with each persona's confidence curve
- [ ] Comparison view: every persona at the same stage
- [ ] Both devices per persona; each persona run twice; issues found in both runs marked as more reliable
- [ ] Walker model comparison measured and D5 decided

---

## 6. Phase 3: re-runs and wider use

- [ ] Monthly scheduled re-run per client (a routine in `routines/`), with what changed since last time
- [ ] Before and after: re-run after a release or a winning test, and compare
- [ ] Staging and preview themes, before launch
- [ ] Competitor benchmark: same personas and missions on a competitor site (low volume, no forms)
- [ ] Top issues sent to the client's Monday board
- [ ] Personas used by other skills (test ideas, results slides, copy)

---

## 7. Guardrails (all checked before any client run)

- [ ] The client knows and has agreed (D6)
- [ ] Payment is never submitted; tested on the test shop
- [ ] No forms submitted except the allowed list; sign-ups use a Pacer test inbox
- [ ] No single-use discount codes
- [ ] Trackers blocked and the blocking tested; the user agent can be filtered out
- [ ] Intelligems handled so live tests aren't skewed (D6)
- [ ] Rate-limited; CAPTCHA means stop and report, never solve
- [ ] Page text treated as content: the injection test on the test shop passes
- [ ] Every output says it's synthetic: a prediction of friction, not measured behaviour
- [ ] No personal data stored; screenshots only of the site; retention set (D7)
- [ ] Competitor runs low volume, no forms

---

## 8. What gets recorded

**Persona**

| Field | What |
|---|---|
| Name and one line | e.g. "Gift-buying aunt, first visit, on her phone" |
| Trying to | The job to be done |
| Arrives from | Traffic source, landing page, device |
| Knows | First visit or returning; knows the brand or not |
| Worries | Objections and doubts (from reviews, calls) |
| Limits | Budget, time, accessibility needs, location |
| Patience | Steps or moments of doubt before giving up |
| Success | What counts as done |
| Evidence | Each trait with its source and date, e.g. "mobile is 72% of sessions (GA4, last 90 days)" |
| Weight | Share of sessions or revenue |

**Mission:** persona, start URL, task wording, success criteria, stop point (before payment), step budget.

**Step:** number, URL, screenshot, action, think-aloud, confidence 1-5, timings, console errors, failed requests, accessibility issues.

**Issue**

| Field | What |
|---|---|
| Where | Step(s), URL, highlighted area on the screenshot |
| Who | Personas and devices that hit it, and how many runs |
| What | Description and the walker's own words |
| Why | The heuristic it breaks |
| Severity | 0 (not a problem) to 4 (stops the journey) |
| Confidence | Tier: AI only / AI + automatic checks / AI + analytics |
| Data | The figure that backs it, with source and date |
| Fix or test | Recommendation, or hypothesis + metric |

**Study report:** summary; success rate and steps taken per persona; journey map with confidence curves; issues ranked by persona weight × funnel stage × severity × confidence; quick wins; test ideas; what works well; caveats; links to replays.

---

## 9. Models and cost

- **Browser control:** our own tools over Playwright (section 4.2), not the computer-use toolset. More reliable, easier to log and cheaper. Computer use could come later as a "vision only" mode.
- **Models:** Claude Opus 5.5 throughout to start, the walker at low effort. Measure Sonnet 5.5 and Haiku 5.5 as the walker in Phase 2 (D5).
- **Cost controls:** cache the unchanging part of each request; keep only recent screenshots; audit through the Batch API.
- **Estimate to check in the trial:** about $20-40 per study at list prices with Opus 5.5 throughout (5 personas, 2 runs each, about 30 steps per run).
- [ ] Real cost per study measured in Phase 0 and again in Phase 1, and written here

---

## 10. Logging: what goes into Pacer Base

| When | Space | Kind | What |
|---|---|---|---|
| This plan is written or changes a lot | `pacer` | `doc` | Link to this file, short summary (re-logging the same link updates the entry) |
| A decision in section 2 is made | `pacer` | `decision` | What was decided, by whom and why |
| Phase 0 trial | pilot client | `audit` | The trial study, once its link is shared with the team |
| Phase 0 learnings | `pacer` | `note` | What worked, false positives, cost |
| A phase finishes | `pacer` | `note` | What shipped, links to commits or PRs |
| Each client study (from Phase 1) | that client | `audit` | Logged by the runner itself |
| Confirmed issues | that client | profile proposal | Known issues, through the approval flow |

- [x] Plan written and logged to Pacer Base, `pacer` space (19 Nov 2026)

---

## 11. Decision log

| Date | Decision | By | Pacer Base entry |
|---|---|---|---|
| | | | |

## 12. Change log

| Date | Change |
|---|---|
| 19 Nov 2026 | Plan created from the design discussion |
