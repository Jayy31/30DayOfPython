#!/bin/sh
set -e
# Migrations are idempotent; run them on every start when Postgres/Supabase is the store.
if [ "${STORE_BACKEND:-memory}" = "postgres" ]; then
  alembic upgrade head
fi
exec python -m engine.main "$@"
