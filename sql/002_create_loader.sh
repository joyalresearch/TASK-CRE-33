
#!/bin/sh
set -eu

: "${IRIS_LOADER_PASSWORD:?IRIS_LOADER_PASSWORD is required}"

psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set ON_ERROR_STOP=1 \
  --set loader_password="$IRIS_LOADER_PASSWORD" <<'SQL'

SELECT format(
    'CREATE ROLE %I LOGIN PASSWORD %L',
    'iris_loader',
    :'loader_password'
)
WHERE NOT EXISTS (
    SELECT 1
    FROM pg_roles
    WHERE rolname = 'iris_loader'
)
\gexec

GRANT CONNECT ON DATABASE iris_local TO iris_loader;

GRANT USAGE ON SCHEMA iris_staging TO iris_loader;

GRANT SELECT, INSERT, UPDATE
ON iris_staging.commercial_poi_staging
TO iris_loader;

GRANT USAGE, SELECT
ON ALL SEQUENCES IN SCHEMA iris_staging
TO iris_loader;

SQL

echo "Restricted loader role initialized."
