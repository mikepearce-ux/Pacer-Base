"""SQL migrations. Append new steps to the end of a list; never edit an applied one.

Two kinds of database:

* the **control** database (the one ``DATABASE_URL`` points at): the client
  registry and API tokens. No client content lives here.
* one **client** database per client (``pb_<slug>``): that client's profile and
  entries, and nothing about any other client.
"""

CONTROL_MIGRATIONS: list[str] = [
    # 1: registry and tokens
    """
    CREATE TABLE clients (
        slug        text PRIMARY KEY,
        name        text NOT NULL,
        aliases     text[] NOT NULL DEFAULT '{}',
        domains     text[] NOT NULL DEFAULT '{}',
        db_name     text NOT NULL UNIQUE,
        status      text NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'paused', 'archived')),
        created_at  timestamptz NOT NULL DEFAULT now(),
        updated_at  timestamptz NOT NULL DEFAULT now()
    );

    CREATE TABLE api_tokens (
        id            bigserial PRIMARY KEY,
        label         text NOT NULL,
        token_hash    text NOT NULL UNIQUE,
        scope         text NOT NULL CHECK (scope IN ('read', 'write', 'admin')),
        clients       text[],            -- NULL means every client
        created_at    timestamptz NOT NULL DEFAULT now(),
        last_used_at  timestamptz,
        revoked_at    timestamptz
    );
    """,
    # 2: an 'approve' scope between write and admin; agency-wide settings
    """
    ALTER TABLE api_tokens DROP CONSTRAINT api_tokens_scope_check;
    ALTER TABLE api_tokens ADD CONSTRAINT api_tokens_scope_check
        CHECK (scope IN ('read', 'write', 'approve', 'admin'));

    -- Approvers, the review channel, agency-wide Slack channels, limits.
    CREATE TABLE settings (
        key         text PRIMARY KEY,
        value       jsonb NOT NULL,
        updated_at  timestamptz NOT NULL DEFAULT now(),
        updated_by  text NOT NULL DEFAULT ''
    );
    """,
]

CLIENT_MIGRATIONS: list[str] = [
    # 1: profile, entries, full-text search
    """
    -- Lets a dumped client database say whose it is.
    CREATE TABLE client_meta (
        key   text PRIMARY KEY,
        value text NOT NULL
    );

    -- The living brief, one row per section (see models.PROFILE_SECTIONS).
    CREATE TABLE profile_sections (
        key         text PRIMARY KEY,
        content     text NOT NULL DEFAULT '',
        updated_at  timestamptz NOT NULL DEFAULT now(),
        updated_by  text NOT NULL DEFAULT ''
    );

    -- Previous versions of a section, written whenever one is replaced.
    CREATE TABLE profile_history (
        id          bigserial PRIMARY KEY,
        key         text NOT NULL,
        content     text NOT NULL,
        updated_at  timestamptz NOT NULL,
        updated_by  text NOT NULL,
        replaced_at timestamptz NOT NULL DEFAULT now()
    );

    -- array_to_string is only STABLE, which a generated column won't accept.
    -- Joining text with spaces doesn't depend on any setting, so this is safe.
    CREATE FUNCTION pb_join_tags(text[]) RETURNS text
        LANGUAGE sql IMMUTABLE PARALLEL SAFE
        AS $$ SELECT coalesce(array_to_string($1, ' '), '') $$;

    -- One thing that happened or was made: a call, a test, a report, a doc...
    CREATE TABLE entries (
        id           bigserial PRIMARY KEY,
        kind         text NOT NULL,
        title        text NOT NULL,
        summary      text NOT NULL DEFAULT '',
        body         text NOT NULL DEFAULT '',
        url          text,
        status       text,
        source       text NOT NULL DEFAULT 'manual',
        external_id  text,
        tags         text[] NOT NULL DEFAULT '{}',
        metadata     jsonb NOT NULL DEFAULT '{}'::jsonb,
        occurred_at  timestamptz NOT NULL DEFAULT now(),
        created_at   timestamptz NOT NULL DEFAULT now(),
        updated_at   timestamptz NOT NULL DEFAULT now(),
        created_by   text NOT NULL DEFAULT '',
        updated_by   text NOT NULL DEFAULT '',
        archived_at  timestamptz,
        search       tsvector GENERATED ALWAYS AS (
            setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
            setweight(to_tsvector('english', pb_join_tags(tags)), 'A') ||
            setweight(to_tsvector('english', coalesce(summary, '')), 'B') ||
            setweight(to_tsvector('english', coalesce(body, '')), 'C')
        ) STORED
    );

    CREATE INDEX entries_search_idx ON entries USING gin (search);
    CREATE INDEX entries_kind_time_idx ON entries (kind, occurred_at DESC);
    CREATE INDEX entries_time_idx ON entries (occurred_at DESC);
    CREATE INDEX entries_url_idx ON entries (url) WHERE url IS NOT NULL;
    CREATE INDEX entries_tags_idx ON entries USING gin (tags);
    CREATE UNIQUE INDEX entries_source_external_idx
        ON entries (source, external_id) WHERE external_id IS NOT NULL;
    """,
    # 2: where to look, links to follow, and changes waiting for approval
    """
    -- Other places to look for more on an entry: [{"label", "url"}].
    ALTER TABLE entries ADD COLUMN related jsonb NOT NULL DEFAULT '[]'::jsonb;

    -- The only places the daily round-up may read for this client. Changed by
    -- admins only, never by the round-up itself.
    CREATE TABLE sources (
        id               bigserial PRIMARY KEY,
        system           text NOT NULL,
        ref              text NOT NULL,
        name             text NOT NULL DEFAULT '',
        url              text,
        role             text NOT NULL DEFAULT '',
        notes            text NOT NULL DEFAULT '',
        UNIQUE (system, ref)
    );

    -- How far the round-up has read each source (and each agency-wide channel)
    -- for this client, so the next run starts where the last one stopped.
    CREATE TABLE sync_state (
        key            text PRIMARY KEY,      -- '<system>:<ref>'
        checked_until  timestamptz NOT NULL,
        updated_at     timestamptz NOT NULL DEFAULT now(),
        updated_by     text NOT NULL DEFAULT ''
    );

    -- A change to a profile section that waits for an approver.
    CREATE TABLE proposals (
        id                bigserial PRIMARY KEY,
        section           text NOT NULL,
        base_content      text NOT NULL,
        proposed_content  text NOT NULL,
        change_summary    text NOT NULL,
        reason            text NOT NULL DEFAULT '',
        evidence_url      text,
        source_keys       text[] NOT NULL DEFAULT '{}',
        status            text NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'superseded')),
        proposed_by       text NOT NULL,
        proposed_at       timestamptz NOT NULL DEFAULT now(),
        review_channel    text,
        review_ts         text,
        decided_by        text,
        decided_via       text,
        decided_at        timestamptz,
        decision_note     text,
        applied_content   text
    );
    CREATE INDEX proposals_status_idx ON proposals (status, section);
    CREATE INDEX proposals_source_keys_idx ON proposals USING gin (source_keys);
    """,
]
