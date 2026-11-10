"""Tests run against a real Postgres server.

Set PACER_BASE_TEST_DATABASE_URL to a superuser connection on a throwaway
server, e.g. ``postgresql://postgres@127.0.0.1:55432/postgres`` (scripts/test-db.sh
starts one in Docker). Each test run creates its own control database and
client databases with a random prefix, and drops them afterwards.
"""

from __future__ import annotations

import os
import secrets
import socket
import threading
import time

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from pacer_base.config import Settings

ADMIN = "test-admin-token"
BASE_URL = os.environ.get("PACER_BASE_TEST_DATABASE_URL", "")

needs_db = pytest.mark.skipif(not BASE_URL, reason="PACER_BASE_TEST_DATABASE_URL not set")


def _admin_conn() -> psycopg.Connection:
    return psycopg.connect(BASE_URL, autocommit=True)


@pytest.fixture
def settings():
    if not BASE_URL:
        pytest.skip("PACER_BASE_TEST_DATABASE_URL not set")
    tag = secrets.token_hex(4)
    control = f"pbtest_{tag}"
    prefix = f"pbt{tag}_"
    with _admin_conn() as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(control)))
    yield Settings(database_url=make_conninfo(BASE_URL, dbname=control), admin_token=ADMIN, client_db_prefix=prefix)
    with _admin_conn() as conn:
        names = [r[0] for r in conn.execute(
            "SELECT datname FROM pg_database WHERE datname = %s OR datname LIKE %s", (control, prefix + "%")
        ).fetchall()]
        for name in names:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def api(settings):
    from fastapi.testclient import TestClient

    from pacer_base.app import create_app

    with TestClient(create_app(settings)) as client:
        yield client


def admin_headers() -> dict:
    return {"Authorization": f"Bearer {ADMIN}"}


def make_token(api, scope="write", clients=None, label=None) -> str:
    r = api.post(
        "/api/tokens",
        json={"label": label or f"{scope}-token", "scope": scope, "clients": clients},
        headers=admin_headers(),
    )
    assert r.status_code == 201, r.text
    return r.json()["token"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def live_server(settings):
    """The real app on a real port, for the MCP client."""
    import uvicorn

    from pacer_base.app import create_app

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)
