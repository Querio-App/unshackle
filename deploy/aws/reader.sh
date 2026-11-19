#!/bin/sh
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv reader_password READER_PASSWORD
CREATE ROLE example_reader LOGIN PASSWORD :'reader_password';
REVOKE ALL ON DATABASE example FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE example TO example_reader;
GRANT USAGE ON SCHEMA public TO example_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO example_reader;
ALTER ROLE example_reader SET default_transaction_read_only = on;
SQL
