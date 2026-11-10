"""Markdown for Claude: compact, scannable, every item carrying its id and link."""

from __future__ import annotations

from datetime import datetime

from .models import KINDS, PROFILE_SECTIONS, SYSTEMS

CONTEXT_CHAR_BUDGET = 24_000
LIST_SUMMARY_CHARS = 500


def _date(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d") if value else "?"


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def entry_line(e: dict, summary_chars: int = LIST_SUMMARY_CHARS, snippet: bool = False) -> str:
    bits = [f"**{e['title']}**", f"{e['kind']}", _date(e.get("occurred_at"))]
    if e.get("status"):
        bits.append(f"status: {e['status']}")
    head = " · ".join(bits)
    lines = [f"- [#{e['id']}] {head}"]
    text = e.get("snippet") if snippet and e.get("snippet") else e.get("summary")
    if text:
        lines.append(f"  {_clip(text, summary_chars)}")
    if e.get("url"):
        lines.append(f"  Link: {e['url']}")
    if e.get("related"):
        lines.append("  Related: " + " · ".join(f"{r['label']} {r['url']}" for r in e["related"]))
    if e.get("tags"):
        lines.append(f"  Tags: {', '.join(e['tags'])}")
    if e.get("archived_at"):
        lines.append("  (archived)")
    return "\n".join(lines)


def entry_full(client: dict, e: dict) -> str:
    out = [f"# {e['title']}", ""]
    meta = [
        f"Client: {client['name']} ({client['slug']})",
        f"Entry: #{e['id']} · {e['kind']}" + (f" · status: {e['status']}" if e.get("status") else ""),
        f"Date: {_date(e['occurred_at'])} (logged {_date(e['created_at'])} by {e['created_by'] or 'unknown'};"
        f" last changed {_date(e['updated_at'])} by {e['updated_by'] or 'unknown'})",
        f"Source: {e['source']}" + (f" · external id: {e['external_id']}" if e.get("external_id") else ""),
    ]
    if e.get("url"):
        meta.append(f"Link: {e['url']}")
    if e.get("tags"):
        meta.append(f"Tags: {', '.join(e['tags'])}")
    if e.get("archived_at"):
        meta.append(f"Archived {_date(e['archived_at'])}")
    out += meta
    if e.get("related"):
        out += ["", "## Where to look for more"] + [f"- {r['label']}: {r['url']}" for r in e["related"]]
    if e.get("summary"):
        out += ["", "## Summary", e["summary"].strip()]
    if e.get("body"):
        out += ["", "## Detail", e["body"].strip()]
    if e.get("metadata"):
        out += ["", "## Metadata"] + [f"- {k}: {v}" for k, v in e["metadata"].items()]
    return "\n".join(out)


def profile_block(profile: dict[str, dict], empty_hint: bool = True) -> str:
    parts = []
    for key, title in PROFILE_SECTIONS.items():
        row = profile.get(key)
        if row and row["content"].strip():
            parts.append(
                f"### {title}\n_(section `{key}`, updated {_date(row['updated_at'])}"
                f"{' by ' + row['updated_by'] if row['updated_by'] else ''})_\n{row['content'].strip()}"
            )
    if not parts and empty_hint:
        return (
            "_No profile written yet. Suggest content with propose_profile_change "
            f"(sections: {', '.join(PROFILE_SECTIONS)}); an approver confirms it._"
        )
    missing = [k for k in PROFILE_SECTIONS if not (profile.get(k) and profile[k]["content"].strip())]
    if missing and parts:
        parts.append(f"_Empty sections: {', '.join(missing)}._")
    return "\n\n".join(parts)


def source_line(x: dict) -> str:
    bits = [f"**{x['system']}**", x["name"] or x["ref"]]
    if x.get("role"):
        bits.append(f"({x['role']})")
    line = "- " + " ".join(bits) + f" · id `{x['ref']}`"
    if x.get("url"):
        line += f" · {x['url']}"
    if x.get("notes"):
        line += f"\n  {x['notes']}"
    return line


def where_to_look(sources: list[dict]) -> str:
    if not sources:
        return "_No sources listed for this client yet (an admin adds them)._"
    return "\n".join(source_line(x) for x in sources)


def proposal_line(p: dict) -> str:
    line = (
        f"- [P{p['id']}] {PROFILE_SECTIONS.get(p['section'], p['section'])}: {p['change_summary']}"
        f" · {p['status']} · proposed {_date(p['proposed_at'])} by {p['proposed_by']}"
    )
    if p.get("evidence_url"):
        line += f" · evidence: {p['evidence_url']}"
    if p.get("decided_at"):
        line += f" · {p['status']} {_date(p['decided_at'])}" + (f" by {p['decided_by']}" if p.get("decided_by") else "")
    if p["status"] == "pending":
        line += " · posted for review" if p.get("review_ts") else " · not yet posted for review"
    return line


def proposal_full(p: dict) -> str:
    out = [proposal_line(p)]
    if p.get("reason"):
        out.append(f"  Why: {p['reason']}")
    out.append("  Current text:\n" + _indent(p["base_content"] or "(empty)"))
    out.append("  Proposed text:\n" + _indent(p["proposed_content"]))
    if p.get("review_channel"):
        out.append(f"  Review message: channel {p['review_channel']}, ts {p['review_ts']}")
    if p.get("decision_note"):
        out.append(f"  Note: {p['decision_note']}")
    return "\n".join(out)


def _indent(text: str) -> str:
    return "\n".join("    " + ln for ln in text.strip().splitlines())


def client_context(
    client: dict,
    profile: dict[str, dict],
    recent: list[dict],
    stats: dict,
    sources: list[dict] | None = None,
    pending: list[dict] | None = None,
) -> str:
    counts = ", ".join(f"{n} {k}" for k, n in stats["by_kind"].items()) or "none yet"
    header = [
        f"# {client['name']}: Pacer Base context",
        "",
        f"Client id: `{client['slug']}`"
        + (f" · also known as: {', '.join(client['aliases'])}" if client["aliases"] else "")
        + (f" · domains: {', '.join(client['domains'])}" if client["domains"] else ""),
        f"Entries: {stats['entries']} ({counts}) · last updated "
        + (_date(stats["last_updated"]) if stats["last_updated"] else "never"),
        "",
        "## Profile",
        profile_block(profile),
    ]
    if pending:
        header += [
            "",
            "## Waiting for approval (unconfirmed: don't treat as fact yet)",
            *[proposal_line(p) for p in pending],
        ]
    header += [
        "",
        "## Where to look",
        "Pacer Base is the map; these systems hold the detail. Follow an entry's links,"
        " or look here, with the matching connector.",
        where_to_look(sources or []),
        "",
        f"## Recent activity (newest {len(recent)})" if recent else "## Recent activity",
    ]
    text = "\n".join(header)
    if not recent:
        return text + "\n_Nothing logged yet._"
    lines = []
    used = len(text)
    for e in recent:
        line = entry_line(e)
        if used + len(line) > CONTEXT_CHAR_BUDGET:
            lines.append(f"_…{len(recent) - len(lines)} more not shown; use list_entries or search._")
            break
        lines.append(line)
        used += len(line) + 1
    footer = "\n\n_For the full text of an entry use get_entry with its #id. Search older items with search_client_context._"
    return text + "\n" + "\n".join(lines) + footer


def client_list(clients: list[dict], stats_by_slug: dict[str, dict]) -> str:
    if not clients:
        return "No clients yet (or none this token can see)."
    lines = ["| Client | Id | Also known as | Entries | Last updated |", "|---|---|---|---|---|"]
    for c in clients:
        s = stats_by_slug.get(c["slug"], {"entries": 0, "last_updated": None})
        status = "" if c["status"] == "active" else f" ({c['status']})"
        lines.append(
            f"| {c['name']}{status} | `{c['slug']}` | {', '.join(c['aliases']) or '-'} |"
            f" {s['entries']} | {_date(s['last_updated']) if s['last_updated'] else 'never'} |"
        )
    return "\n".join(lines)


def kinds_help() -> str:
    return "; ".join(f"{k}: {v}" for k, v in KINDS.items())


def systems_help() -> str:
    return "; ".join(f"{k}: {v}" for k, v in SYSTEMS.items())
