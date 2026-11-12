"""Settings, all from environment variables (Railway sets them on the service)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # Connection string for the control database (the client registry and tokens).
    # Each client's own database lives on the same Postgres server, named
    # ``<client_db_prefix><slug>``.
    database_url: str
    # Bootstrap admin token. Anyone presenting it can create clients and tokens.
    admin_token: str = ""
    client_db_prefix: str = "pb_"
    port: int = 8000
    # The public https address, if it can't be worked out from the request
    # (used in the automatic sign-in handshake's metadata).
    public_url: str = ""
    # Attention Lab (heat maps), for the /heatmap/mcp connector.
    attention_lab_url: str = ""
    attention_lab_token: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        url = os.environ.get("PACER_BASE_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
        if not url:
            raise RuntimeError(
                "No database configured. Set DATABASE_URL (on Railway: ${{Postgres.DATABASE_URL}})."
            )
        return cls(
            database_url=url,
            admin_token=os.environ.get("PACER_BASE_ADMIN_TOKEN", ""),
            client_db_prefix=os.environ.get("PACER_BASE_CLIENT_DB_PREFIX", "pb_"),
            port=int(os.environ.get("PORT", "8000")),
            public_url=os.environ.get("PACER_BASE_PUBLIC_URL", ""),
            attention_lab_url=os.environ.get("ATTENTION_LAB_URL", ""),
            attention_lab_token=os.environ.get("ATTENTION_LAB_TOKEN", ""),
        )
