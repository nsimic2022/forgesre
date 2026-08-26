#!/bin/sh
# Create database netbox + role on the existing ForgeSRE Postgres.
# Idempotent. Does not drop or rewrite the forgesre database.
set -eu

if [ -z "${POSTGRES_PASSWORD:-}" ] || [ -z "${NETBOX_DB_PASSWORD:-}" ]; then
  echo "netbox-db-init: POSTGRES_PASSWORD and NETBOX_DB_PASSWORD are required." >&2
  exit 1
fi

export PGPASSWORD="${POSTGRES_PASSWORD}"

echo "Waiting for Postgres on 127.0.0.1..."
i=0
while [ "$i" -lt 60 ]; do
  if pg_isready -h 127.0.0.1 -U forgesre >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 2
done
pg_isready -h 127.0.0.1 -U forgesre

psql -h 127.0.0.1 -U forgesre -d postgres -v ON_ERROR_STOP=1 <<EOF
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'netbox') THEN
    CREATE ROLE netbox LOGIN PASSWORD '${NETBOX_DB_PASSWORD}';
  ELSE
    ALTER ROLE netbox WITH LOGIN PASSWORD '${NETBOX_DB_PASSWORD}';
  END IF;
END
\$\$;
SELECT 'CREATE DATABASE netbox OWNER netbox'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'netbox')\gexec
GRANT ALL PRIVILEGES ON DATABASE netbox TO netbox;
EOF

psql -h 127.0.0.1 -U forgesre -d netbox -v ON_ERROR_STOP=1 <<EOF
GRANT ALL ON SCHEMA public TO netbox;
ALTER SCHEMA public OWNER TO netbox;
EOF

echo "NetBox database ready (Postgres database netbox, role netbox)."
