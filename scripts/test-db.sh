#!/usr/bin/env bash
# Start a throwaway Postgres 16 for the tests, then print the variable to set.
#   ./scripts/test-db.sh && export PACER_BASE_TEST_DATABASE_URL=... && pytest -q
set -euo pipefail
NAME=pacer-base-test-db
PORT=${PORT:-55432}
if ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
  docker run -d --rm --name "$NAME" -e POSTGRES_PASSWORD=postgres -p "$PORT:5432" postgres:16 >/dev/null
  until docker exec "$NAME" pg_isready -U postgres >/dev/null 2>&1; do sleep 0.5; done
fi
echo "export PACER_BASE_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:$PORT/postgres"
