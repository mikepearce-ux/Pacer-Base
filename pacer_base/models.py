"""Shared vocabulary and input validation (used by both the REST API and the MCP tools)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# What an entry can be. Kept short on purpose so filtering ("all tests") works.
KINDS: dict[str, str] = {
    "call": "A call or meeting with the client (or internal, about them): notes, decisions, actions",
    "test": "An A/B or CRO test: hypothesis, setup, status, result",
    "report": "A performance report, results slide or read-out",
    "audit": "A CRO audit, diagnostic or research piece",
    "proposal": "A proposal, roadmap, SOW or pitch",
    "doc": "Any other document worth finding again (strategy, brief, spec)",
    "decision": "A decision and why it was made",
    "note": "Anything else worth remembering",
}
Kind = Literal["call", "test", "report", "audit", "proposal", "doc", "decision", "note"]

# The client profile ("brief"), in display order.
PROFILE_SECTIONS: dict[str, str] = {
    "overview": "Overview",
    "people": "Who's who",
    "goals": "Goals",
    "current_tests": "Current tests",
    "numbers": "Numbers we know",
    "issues": "Known issues",
    "promises": "What we've promised",
    "links": "Key links",
}
ProfileSection = Literal[
    "overview", "people", "goals", "current_tests", "numbers", "issues", "promises", "links"
]

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,40}$")

MAX_TITLE = 300
MAX_SUMMARY = 4_000
MAX_BODY = 100_000
MAX_URL = 2_000
MAX_SECTION = 20_000


def slugify(name: str) -> str:
    """'S&S' -> 's-and-s', 'Living Planet' -> 'living-planet'."""
    text = name.lower().replace("&", " and ").replace("+", " plus ")
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return slug[:41].strip("-") or "client"


def _check_url(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > MAX_URL:
        raise ValueError(f"url is longer than {MAX_URL} characters")
    if not re.match(r"^https?://[^\s]+$", value, re.IGNORECASE):
        raise ValueError("url must start with http:// or https:// and contain no spaces")
    return value


def _clean_tags(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    out: list[str] = []
    for tag in value:
        tag = re.sub(r"\s+", "-", str(tag).strip().lower())[:50]
        if tag and tag not in out:
            out.append(tag)
    if len(out) > 20:
        raise ValueError("at most 20 tags")
    return out


# Systems a client's sources can live in.
SYSTEMS: dict[str, str] = {
    "slack": "Slack channel (ref: channel id)",
    "granola": "Granola meetings (ref: attendee email domain)",
    "drive": "Google Drive folder (ref: folder id); everything in it counts as approved",
    "monday": "Monday board (ref: board id)",
    "intelligems": "Intelligems organisation (ref: organisation id)",
    "airtable": "Airtable base (ref: base id)",
    "shopify": "Shopify store (ref: myshopify domain)",
    "ga4": "GA4 property (ref: property id)",
    "other": "Anything else (ref: a url or name)",
}
System = Literal["slack", "granola", "drive", "monday", "intelligems", "airtable", "shopify", "ga4", "other"]


class RelatedLink(BaseModel):
    """Somewhere else to look for more: a Monday item, Slack thread, test, doc."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=80)
    url: str

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        checked = _check_url(v)
        if not checked:
            raise ValueError("related link needs a url")
        return checked


class EntryIn(BaseModel):
    """A new entry, or the fields to change on an existing one.

    Fields left as None keep their current value on an update.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Kind | None = None
    title: str | None = Field(default=None, max_length=MAX_TITLE)
    summary: str | None = Field(default=None, max_length=MAX_SUMMARY)
    body: str | None = Field(default=None, max_length=MAX_BODY)
    url: str | None = None
    status: str | None = Field(default=None, max_length=40)
    source: str | None = None
    external_id: str | None = Field(default=None, max_length=200)
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None
    related: list[RelatedLink] | None = Field(default=None, max_length=10)
    occurred_at: datetime | None = None

    @field_validator("url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return _check_url(v)

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str] | None) -> list[str] | None:
        return _clean_tags(v)

    @field_validator("source")
    @classmethod
    def _source(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip().lower()
        if not SOURCE_RE.match(v):
            raise ValueError("source must be a short lower-case name like 'granola' or 'intelligems'")
        return v

    @field_validator("occurred_at")
    @classmethod
    def _tz(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("occurred_at needs a timezone, e.g. 2026-11-04T10:00:00Z")
        return v

    def require_new(self) -> None:
        """Checks that only apply when creating an entry."""
        missing = [f for f in ("kind", "title") if not getattr(self, f)]
        if missing:
            raise ValueError(f"A new entry needs: {', '.join(missing)}")
        if not (self.summary or self.body or self.url):
            raise ValueError("A new entry needs at least a summary, a body or a url")


class ClientIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    slug: str | None = None
    aliases: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        if not SLUG_RE.match(v):
            raise ValueError("slug must be lower-case letters, digits and dashes (max 41)")
        return v

    @field_validator("aliases")
    @classmethod
    def _aliases(cls, v: list[str]) -> list[str]:
        return [a.strip() for a in v if a and a.strip()]

    @field_validator("domains")
    @classmethod
    def _domains(cls, v: list[str]) -> list[str]:
        return [d.strip().lower().lstrip("@") for d in v if d and d.strip()]

    def resolved_slug(self) -> str:
        return self.slug or slugify(self.name)


class ClientUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=120)
    aliases: list[str] | None = None
    domains: list[str] | None = None
    status: Literal["active", "paused", "archived"] | None = None

    @field_validator("aliases")
    @classmethod
    def _aliases(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else [a.strip() for a in v if a and a.strip()]

    @field_validator("domains")
    @classmethod
    def _domains(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else [d.strip().lower().lstrip("@") for d in v if d and d.strip()]


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(max_length=MAX_SECTION)


class TokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=80)
    scope: Literal["read", "write", "approve", "admin"] = "read"
    clients: list[str] | None = None


class SourceIn(BaseModel):
    """One place the daily round-up may read for a client."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    system: System
    ref: str = Field(min_length=1, max_length=200)
    name: str = Field(default="", max_length=120)
    url: str | None = None
    role: str = Field(default="", max_length=120)
    notes: str = Field(default="", max_length=1000)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return _check_url(v)


class SourcesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[SourceIn] = Field(max_length=50)


class Approver(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=80)
    slack_user_id: str = Field(pattern=r"^[UW][A-Z0-9]{4,20}$")


class ChannelRef(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=r"^[CG][A-Z0-9]{4,20}$")
    name: str = Field(default="", max_length=80)
    notes: str = Field(default="", max_length=300)


# Agency-wide settings and the shape each must have.
SETTING_TYPES: dict[str, Any] = {
    "approvers": list[Approver],
    "review_channel": ChannelRef,
    "agency_channels": list[ChannelRef],
    "daily_entry_cap": int,
    "proposal_expiry_days": int,
}
SETTING_DEFAULTS: dict[str, Any] = {
    "approvers": [],
    "review_channel": None,
    "agency_channels": [],
    "daily_entry_cap": 50,
    "proposal_expiry_days": 7,
}


class ProposalIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    section: ProfileSection
    proposed_content: str = Field(min_length=1, max_length=MAX_SECTION)
    change_summary: str = Field(min_length=1, max_length=500)
    reason: str = Field(default="", max_length=2000)
    evidence_url: str | None = None
    source_keys: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("evidence_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return _check_url(v)

    @field_validator("source_keys")
    @classmethod
    def _keys(cls, v: list[str]) -> list[str]:
        return [k.strip()[:200] for k in v if k and k.strip()]


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: Literal["approve", "reject"]
    decided_by: str = Field(min_length=1, max_length=80)
    via: str = Field(default="api", max_length=80)
    corrected_content: str | None = Field(default=None, max_length=MAX_SECTION)
    note: str | None = Field(default=None, max_length=1000)
